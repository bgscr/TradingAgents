# Mainland qualified routing production composition factory

Status: ready-for-agent

## Problem Statement

`main` contains the complete mainland capability-routing and financial-period-
eligibility behavior, and deterministic `qualified_v1_shadow` acceptance passes.
The real shadow preflight still terminates with
`qualified_routing_composition_missing` because the host can load only an
explicit operator/test factory or accept an injected composition. There is no
shipped factory that assembles the qualified adapters and owns their runtime
resources.

This is a composition and lifecycle gap, not a policy gap. The Capability
Routing Plan, completeness and financial eligibility policies, Evidence
Admission, Decision Gate, BaoStock status authority, daily mainland market
routing, and `shadow_non_authoritative` disposition are already authoritative.
A solution must make real, explicitly requested `qualified_v1_shadow` workloads
constructible without turning the factory into a second routing-policy source or
creating unowned provider state.

## Solution

Ship and statically register one built-in qualified routing composition factory.
For an explicitly requested validation workload whose immutable Capability
Routing Plan is `qualified_v1_shadow`, the factory assembles the existing
Tushare, AKShare-Sina, Yahoo, and BaoStock adapters behind one run-owned
composition. It binds every physical request to the existing Provider Request
Coordinator, Provider Subrequest Cache, immutable artifact store, financial
dispatcher, checkpoint, and graph seams.

The factory has two deterministic phases under one owner:

1. Before graph, model, provider, database, artifact, or report work, validate
   the mode, plan, Instrument Identity, configuration, dependencies, credential
   availability, and factory contract, then create an immutable composition
   blueprint.
2. When the existing graph/dispatcher seam has the canonical run identity and
   compatible checkpoint projection, bind exactly one run runtime containing
   the coordinator authority, restored Provider Subrequest Cache, artifact
   store, provider clients/transports, adapters, router sources, and request
   factories.

The managed composition owns idempotent interruption and close behavior from
successful construction until normal completion or any `BaseException`. It
creates no process-global mutable provider state. Legacy mode creates no
qualified composition. Authoritative `qualified_v1` remains blocked in
preflight and is also rejected defensively by the built-in factory.

Existing override behavior is preserved with this precedence:

1. A directly injected composition supplied by a programmatic/operator caller.
2. An explicit `MODULE:CALLABLE` operator factory.
3. The statically registered built-in production factory.

The word “production” here means shipped, non-test construction code. It does
not change the configured Data Usage Mode or grant provider usage rights.

## User Stories

1. As a shadow-run operator, I want a shipped factory to be selected without test injection, so that real qualified shadow preflight can proceed.
2. As a CLI operator, I want explicit qualified shadow validation to use the shipped composition, so that the CLI can exercise the real adapters.
3. As a programmatic caller, I want the existing analysis entry point and composition builder to resolve the same shipped factory, so that CLI and programmatic behavior do not diverge.
4. As an operator with a custom factory, I want my explicit override to retain precedence, so that controlled validation and local integrations remain possible.
5. As a test author, I want direct composition injection to retain highest precedence, so that deterministic tests never require live providers.
6. As a legacy-mode user, I want no qualified composition or qualified provider work constructed, so that the default path remains unchanged.
7. As a rollout owner, I want `qualified_v1` rejected before factory construction, so that adding production wiring cannot activate authority.
8. As a routing-policy owner, I want the immutable Capability Routing Plan to determine every provider sequence, so that construction code cannot reorder routes.
9. As a fundamentals user, I want existing Tushare statement and indicator adapters assembled, so that qualified data can be acquired through their accepted contracts.
10. As a fallback user, I want existing AKShare-Sina statement adapters assembled, so that unresolved Tushare periods can continue sequentially.
11. As a degraded-evidence user, I want existing Yahoo fallbacks assembled and labeled with their current-only or derived limitations, so that degraded evidence is not overstated.
12. As a mainland risk owner, I want BaoStock capability and ratio adapters assembled without changing BaoStock status authority, so that Current Tradeability remains trustworthy.
13. As an audit reader, I want adjustment-factor and issuer-name routes composed independently, so that name text never creates suspension, ST, listing, or tradeability authority.
14. As a strict-replay user, I want BaoStock bundle semantics preserved, so that factors from one provider are never combined with another provider’s raw observations or status.
15. As an operator, I want every Tushare, AKShare-Sina, Yahoo, and BaoStock physical request coordinated, so that Safety Ceilings, cooldowns, retries, and physical-attempt counts remain authoritative.
16. As an operator, I want identical requests single-flighted and bulk responses reused, so that one logical request does not produce redundant provider traffic.
17. As a checkpoint user, I want the Provider Subrequest Cache restored before qualified dispatch, so that an exact resume performs no external provider or model I/O.
18. As a checkpoint user, I want incompatible or corrupt restored state rejected before work, so that a run cannot mix plans, eligibility policies, identities, artifacts, or attempt histories.
19. As an artifact reader, I want existing content-addressed artifacts and IDs reused, so that the factory introduces no competing durability format.
20. As an operator, I want configuration and dependency failures reported before work begins, so that a bad composition cannot leave partial runtime state.
21. As an operator interrupting a run, I want new provider work stopped and owned resources released, so that interruption does not leak sessions, leases, connections, or workers.
22. As a maintainer diagnosing a graph failure, I want the composition closed exactly once, so that cleanup is deterministic across initialization and execution failures.
23. As a security owner, I want Tushare credentials read only from the existing environment boundary, so that tokens never enter config, checkpoints, reports, artifacts, or IDs.
24. As a security owner, I want provider exceptions translated into existing typed outcomes, so that raw provider errors and secrets cannot cross trusted boundaries.
25. As a concurrency user, I want provider clients and caches owned by the run rather than module globals, so that parallel runs cannot mutate shared factory state.
26. As an audit owner, I want legacy-selected output to remain authoritative in shadow, so that qualified failures or successes cannot change the Trading Decision.
27. As a compatibility owner, I want historical reports, checkpoints, artifacts, attempts, and IDs left untouched, so that adding the factory is additive.
28. As a market-data owner, I want daily mainland acquisition to remain AKShare, BaoStock, then Yahoo, so that financial composition does not alter price semantics.
29. As a verification owner, I want exact deterministic acceptance for each adapter, fallback, cache, lifecycle, and safety boundary, so that the factory is proven at the highest existing seam.
30. As a rollout owner, I want a final real two-symbol shadow gate covering a bank and a non-bank Equity, so that shipped construction is proven before any later activation review.
31. As a rollout owner, I want passing the real shadow gate to leave `qualified_v1` disabled, so that observation is not confused with authority activation.
32. As a maintainer, I want PE1–PE15 and parent AC1–AC13 rerun unchanged, so that the factory cannot weaken already accepted financial-period or market-data behavior.

## Implementation Decisions

### Scope and authority invariants

- This feature supplies construction, registration, lifecycle, and restoration
  only. The existing Capability Routing Plan is the sole policy authority.
- The factory must consume the plan’s exact routes and enabled capabilities. It
  must not contain a second provider-order table used for execution, silently
  fill an absent plan source, or infer policy from installed dependencies.
- Existing financial eligibility, completeness, revision selection, Evidence
  Admission, Decision Gate, Strategy Rules, and reporting behavior are
  unchanged.
- Existing daily mainland routing remains AKShare, BaoStock, then Yahoo. The
  qualified composition is not registered as a daily market-snapshot source and
  does not start foreground work for prewarming or future replay.
- Existing `shadow_non_authoritative` behavior is immutable: legacy dispatch is
  authoritative, qualified results are shadow observations, and any qualified
  exception is contained and sanitized without changing legacy output.
- `qualified_v1` remains disabled at configuration/preflight. The built-in
  factory accepts only a `qualified_v1_shadow` plan plus an explicit validation
  workload, providing defense in depth if called directly.

### Registration and caller precedence

- The existing host composition builder owns one static registration for the
  built-in factory. A normal qualified shadow validation no longer needs a
  factory path in configuration and does not use dynamic import to find the
  built-in.
- Directly injected compositions remain untouched and take first precedence.
- The existing explicit operator factory option remains supported and takes
  precedence over the built-in. The factory reference is removed from the safe
  config passed into that operator factory as it is today.
- If no override is present, the built-in is selected only when the immutable
  plan is `qualified_v1_shadow`, at least one qualified capability requires a
  composition, and the caller explicitly requested the validation workload.
- Legacy mode, an empty/non-applicable plan, and ordinary non-validation shadow
  analysis create no qualified composition, no provider client, no coordinator
  store, no cache, and no qualified artifact directory.
- The CLI commands and the programmatic analysis entry point use the same host
  builder. Programmatic callers may still build the composition explicitly and
  pass it to the graph; graph construction itself never performs provider I/O.

### Single ownership and lifecycle

The top-level factory returns one managed, plan-bound composition. Ownership is
not split among the CLI, graph, adapters, or global modules.

| Concern | Owner and required behavior |
| --- | --- |
| Immutable wiring | The built-in factory validates the plan and creates the source/request blueprint once. |
| Run binding | The composition binds one runtime only after canonical run identity and checkpoint compatibility are known. Rebinding the same run returns the same runtime; binding a different run requires a different composition execution scope. |
| Coordinator authority | The bound runtime opens the existing provider-request authority and owns its store and peer connections. |
| Provider cache and artifacts | The bound runtime creates or restores one Provider Subrequest Cache and one existing immutable artifact store shared by every qualified adapter in the run. |
| Provider clients/transports | The bound runtime owns per-run Tushare, AKShare-Sina, Yahoo, and BaoStock client/transport objects. It does not set SDK module globals or create mutable factory registries. |
| Normal completion | The analysis owner closes the composition after checkpoint/report completion and before returning to post-run interaction. |
| Graph initialization/execution failure | The analysis owner interrupts and closes the composition for every `BaseException`, including `KeyboardInterrupt` and `SystemExit`, whether failure occurs before or after graph compilation. |
| Partial construction failure | A construction-local exit stack closes every resource created before the failure; no partially constructed composition is returned. |
| Repeated cleanup | `interrupt` and `close` are idempotent. Close performs reverse-order cleanup and may not mask the original failure with a cleanup exception. |

Interruption sets a run-owned stop signal before cleanup. New adapter/cache work
is rejected, coordinator wait/pacing loops observe the stop and terminate with an
existing typed, secret-free abandoned/interrupted outcome, in-flight terminal
state is not published as available, BaoStock is logged out, provider sessions
are closed, coordinator peer connections and the authority store are closed, and
all factory-owned worker activity is joined. Normal close performs the same
resource release without manufacturing a failure outcome.

### Deterministic construction and configuration boundary

- Validate mode, explicit validation intent, plan signature, Instrument Identity,
  financial eligibility binding, Data Usage Mode, runtime roots, safety ceilings,
  pacing, account scope, enabled capabilities, required dependencies, provider
  source coverage, and credential presence before opening durable stores or
  constructing graph/model/provider objects.
- Construction errors cross the host boundary only as an existing typed,
  payload-free capability-routing preflight failure. They occur before run
  artifacts, reports, checkpoints, provider attempts, model clients, or provider
  sessions are created.
- The Tushare token is read only from `TUSHARE_TOKEN`, through the existing
  environment boundary, and is passed directly to a per-run client constructor.
  It is never copied into the runtime config, plan, object representation,
  account-scope identity, cache key, artifact, checkpoint, log, report, audit,
  error, or physical-attempt event. Do not use a global token setter.
- The composition captures immutable safe configuration values at construction.
  Later mutation of the input mapping or process environment cannot change its
  route plan, identities, pacing, Safety Ceilings, or Data Usage Mode.
- Time, sleeping, owner identity, and provider transports retain injectable
  boundaries for deterministic tests, but the shipped registration supplies
  production implementations.

### Adapter and route composition

The factory supplies source functions for the provider IDs already named by the
Capability Routing Plan. The router iterates the plan; the factory does not
reorder it. The existing v1 policy currently results in these compositions:

| Capability | Existing adapters assembled | Preserved boundary |
| --- | --- | --- |
| Balance sheet, income statement, cash flow | Tushare statement, AKShare-Sina statement, Yahoo financial fallback | Only unresolved eligible periods continue; usable higher-provider periods survive; Yahoo remains degraded/current-only where its metadata cannot satisfy the strict contract. |
| Financial indicators | Tushare indicator, existing AKShare financial source, BaoStock qualified ratio-family adapter, Yahoo current-profile fallback | BaoStock is requested only for unresolved qualified families/periods; Yahoo remains candidate-free degraded current profile when required by its contract. |
| Adjustment factors | BaoStock capability/bundle adapter, Tushare factor adapter, existing AKShare factor source, Yahoo-derived fallback | Native/strict authority and bundle atomicity remain unchanged; Yahoo-derived factors are always `derived` and `degraded`. |
| Suspension status / Current Tradeability | BaoStock capability adapter plus only supplemental sources already permitted by the plan | Only BaoStock status may establish Current Tradeability. |
| Issuer name events | Tushare name adapter and other sources already named by the plan | Names are descriptive; they establish no ST, suspension, lifecycle, tradeability, Evidence Admission, or Decision Gate fact. |
| Issuer lifecycle | BaoStock capability adapter and any sources already named by the plan | BaoStock lifecycle/status authority remains unchanged. |

Adapter result conversion uses the existing provider-response, manifest,
selection, completeness, and capability-response contracts. No raw frame or SDK
object crosses the adapter boundary. Factor and name calls share the run cache
but retain distinct endpoint scopes, subrequest keys, artifacts, outcomes, and
authority.

BaoStock transport ownership is per run. Login/session creation is lazy and
serialized inside the run-owned transport; logout happens on terminal request
failure, interruption, partial construction failure, and close. Tushare uses a
per-run API client, AKShare-Sina calls are made through a run-owned facade, and
Yahoo creates request-local objects/sessions through the existing coordinated
transport path. The factory creates no mutable module singleton.

### Coordinator, attempt accounting, cache, and bulk reuse

- Every physical provider call, including SDK calls hidden behind a high-level
  adapter, must enter through the Provider Subrequest Cache and Provider Request
  Coordinator. An adapter may not retry, fall back, or call Yahoo internally
  around the coordinator.
- Provider IDs, Upstream Service Identities, account scopes, endpoint capacity
  scopes, priorities, Safety Ceilings, cooldowns, and retry budgets remain those
  already defined by the plan, adapters, and ADR-0031.
- The run has one cache instance shared across statement, indicator, factor,
  name, status, lifecycle, ratio-family, and Yahoo qualified calls. Identical
  subrequest keys therefore share one in-flight future and one terminal result.
- A bulk response is acquired once and reused for every annual/reporting target,
  family, field, or graph tool call covered by that artifact. Selection work may
  repeat deterministically; physical acquisition may not.
- Each real transport invocation produces exactly one physical-attempt event.
  Cache hits, restored results, skipped providers, normalized rows, selected
  periods, and derived facts produce zero physical-attempt events. Retries
  produce exactly the configured additional events, and sequential fallback
  begins only after the preceding source has a terminal non-usable result.
- Attempt IDs, sequence IDs, subrequest keys, artifact IDs, and reuse counts use
  existing constructors and schemas. The factory introduces no replacement ID
  namespace.

### Dispatcher and checkpoint restoration

- The highest shared binding seam is the existing checkpoint-aware financial
  tool/dispatcher construction seam. It already sees the authoritative
  Instrument Identity, canonical run ID, active plan, authoritative and shadow
  ledgers, and checkpoint compatibility before dispatch.
- For `qualified_v1_shadow`, the authoritative dispatcher remains legacy and
  receives no qualified router. A separate shadow dispatcher receives the bound
  qualified router, request factories, and the single restored Provider
  Subrequest Cache.
- The existing shadow financial dispatch ledger remains the owner of qualified
  selection progress and the Provider Subrequest Cache checkpoint. No second
  checkpoint file or sidecar schema is introduced.
- On resume, validate the asset configuration, plan signature, eligibility-policy
  binding, run scope, provider-chain bindings, and cache checkpoint before
  constructing callable adapters. Restore terminal cache entries from existing
  immutable artifact references, then restore dispatcher results. An exact
  repeated request returns from restored state with zero provider transports,
  zero coordinator permits, zero new attempt events, and zero model calls.
  Local checkpoint/artifact reads are the restoration boundary, not external
  provider I/O.
- A missing, malformed, incompatible, or corrupt cache/artifact projection fails
  typed before provider/model work. It does not fall back to a live call, delete
  the bad artifact, mutate the old checkpoint, or reuse only a convenient subset.
- Interrupted in-flight entries are never serialized as available. Checkpointing
  occurs only at the existing safe terminal boundary; otherwise the existing
  unsafe-checkpoint failure is preserved.
- Legacy checkpoints remain readable by the legacy path. Pre-v2 or mismatched
  qualified checkpoints follow the existing start-new/fail-typed rules. No
  historical checkpoint receives an in-place upgrade.

### Historical and shadow compatibility

- Do not rewrite, relabel, migrate in place, delete, or renumber historical
  reports, checkpoints, artifacts, provider rows, physical attempts, snapshots,
  pins, manifests, audits, run IDs, request IDs, sequence IDs, or artifact IDs.
- New runs use existing schemas and ID constructors. The static factory identity
  may be reported as safe operational metadata, but it is not part of evidence,
  route policy, or an existing content identity unless an already versioned
  contract explicitly requires it.
- Qualified shadow manifests, reports, checkpoints, and decision audits continue
  to say `shadow_non_authoritative`. They cannot replace legacy-selected tool
  output, admitted Source Facts, Validated Decision Context, or the final Trading
  Decision.
- PE1–PE15 and parent AC1–AC13 retain their existing text, fixtures, expected
  results, and authority. This feature adds acceptance; it does not amend those
  criteria.

## Testing Decisions

### Test philosophy and seams

- Deterministic tests use fixed clocks, fake sleepers, fake SDK/transport objects,
  disposable existing stores, retained immutable artifacts, and fake models.
  They make no live-provider calls and assert observable contracts rather than
  private constructor order.
- The principal end-to-end seam is the existing host builder through CLI or the
  programmatic analysis entry point, graph construction, checkpoint-aware
  financial tool node, dispatcher, qualified router, adapters, Provider
  Subrequest Cache, and Provider Request Coordinator.
- Adapter-specific tests remain prior art for payload normalization and typed
  outcomes. Routing-selection tests remain prior art for sequential salvage and
  authority. Provider coordinator/cache tests remain prior art for attempts,
  single-flight, and restoration. Rollout, checkpoint, report, decision-audit,
  and corrected-shadow joint acceptance remain prior art for shadow authority
  and compatibility.
- The real two-symbol gate is deliberately separate from deterministic
  acceptance. It runs only after all non-live criteria pass and only with
  explicit operator authorization.

### Deterministic acceptance criteria

| ID | Scenario | Required result |
| --- | --- | --- |
| QCF1 | Start CLI and programmatic `qualified_v1_shadow` validation with qualified capabilities enabled, no injected composition, and no explicit factory path. | The statically registered built-in factory is selected, the plan-bound managed composition reaches the graph, and `qualified_routing_composition_missing` is absent. No test module or dynamic-import fixture participates. |
| QCF2 | Supply a direct composition, then separately an explicit operator factory. | Direct composition wins without invoking either factory; explicit operator factory wins over the built-in; the existing safe-config and plan-signature validations remain exact. |
| QCF3 | Run legacy mode, ordinary non-validation shadow mode, an empty/non-applicable qualified plan, and attempted `qualified_v1`. | Legacy/non-validation paths construct no qualified resource and perform zero qualified I/O; `qualified_v1` fails with the existing authority-disabled preflight before the built-in factory is called. |
| QCF4 | Return complete fixed Tushare statement and indicator fixtures for all eligible targets. | Tushare is selected where the plan names it first; all expected annual/reporting periods and indicator families are retained and selected; no lower provider is called; physical attempts equal the exact Tushare endpoint calls. |
| QCF5 | Make one Tushare endpoint unavailable and another return usable rows plus partial eligible-period/family gaps. | Successful Tushare artifacts and periods survive; only unresolved statement periods or indicator families continue; typed gaps and attempts are exact; no successful bulk request is repeated per target. |
| QCF6 | Leave statement gaps after Tushare and make AKShare-Sina return usable periods, including partial salvage across its statement types. | AKShare-Sina is called sequentially only for unresolved work, its usable artifacts/periods are retained, and Yahoo is not called for resolved targets. |
| QCF7 | Leave eligible statement or indicator gaps after higher providers and provide only Yahoo fallback data. | Yahoo is last, coordinated, and selected only under existing degraded/current-only contracts; candidate-free current profile and Yahoo-derived factor behavior remain exact; no Yahoo evidence becomes strict PIT or native factor authority. |
| QCF8 | Exercise BaoStock raw/factor/status/lifecycle and ratio-family fixtures with tradeable and suspended latest sessions. | BaoStock alone establishes Current Tradeability; strict bundle identity requires its complete three-component binding; missing ratio families are requested narrowly; Tushare names and other sources cannot establish status. |
| QCF9 | Exercise Adjustment Factors and Name Events together, with Tushare factor success, Tushare name success, BaoStock factor success/failure, and Yahoo-derived terminal fallback. | Factor and name requests use distinct plan routes, endpoint scopes, keys, artifacts, and attempts while sharing one cache; name output remains descriptive; Yahoo factor output is `derived` and `degraded`; provider bundles are never blended. |
| QCF10 | Run complete, retryable-failure, terminal-failure, cooldown-skip, partial-fallback, and restored-cache matrices for every assembled provider. | Every actual transport call has one and only one attempt event; retries and fallback match the configured exact cardinality; cache hits, skips, normalization, selection, and restore add zero attempts; no hidden adapter retry or fan-out exists. |
| QCF11 | Concurrently issue identical subrequests and request multiple periods/families/graph calls covered by one bulk artifact. | One leader performs the physical sequence, followers receive the same terminal result, bulk payloads are acquired once, reuse counts are deterministic, and differing material keys do not collide. |
| QCF12 | Checkpoint after terminal qualified shadow work, close all resources, reconstruct through the built-in factory, and repeat the exact request. | Plan, eligibility binding, dispatcher state, cache entries, artifacts, candidates, selections, attempts, and IDs restore exactly; the repeat causes zero provider/network/model I/O, zero coordinator permits, and zero new attempts. Corrupt/mismatched controls fail typed before work. |
| QCF13 | Supply missing token/dependency, invalid mode, invalid plan/asset signature, invalid pacing/Safety Ceiling/account scope/Data Usage Mode/runtime root, incomplete source coverage, and unsafe factory output. | Each failure is secret-free and occurs before graph/model/provider work, store/artifact/report creation, or physical attempts. No partial composition escapes. |
| QCF14 | Inject failure after each construction step, during graph initialization, during provider dispatch, during graph execution, during checkpoint publication, and interrupt with `KeyboardInterrupt`. | The original failure remains primary; interrupt/close occur exactly once; new work stops; all sessions, BaoStock login, coordinator connections/store, leases, and workers are released or terminally abandoned; repeated close is harmless; no process-global mutable provider state remains. |
| QCF15 | Put tokens, credential-like text, URLs with credentials, and raw provider exception text in fake environment values and transport failures. | Tokens and raw errors are absent from config, plan, repr, IDs, events, exceptions, logs, reports, checkpoints, artifacts, manifests, audits, and terminal output; only existing typed safe diagnostics cross boundaries. |
| QCF16 | Compare legacy output, qualified shadow output, daily market acquisition, Evidence Admission, Decision Gate, report, checkpoint, and decision audit before and after built-in registration. | Legacy-selected output and daily AKShare -> BaoStock -> Yahoo routing are unchanged; qualified output remains `shadow_non_authoritative`; no factory result changes admitted evidence or decision authority. |
| QCF17 | Open representative historical reports, checkpoints, artifacts, attempts, manifests, audits, snapshots, pins, and IDs, then create a new built-in-factory run. | Historical material is byte-for-byte untouched and remains readable; new material uses existing schemas/identities; no migration, rewrite, deletion, or ID renumbering occurs. |
| QCF18 | Run PE1–PE15 and parent AC1–AC13 with their existing fixtures and expected results, followed by the full supported suite, Ruff, and whitespace/diff checks. | Every criterion passes unchanged and without waiver. Parent joint acceptance is run twice as already required; any failure blocks the real rollout gate. |

### Final real two-symbol shadow rollout gate

This is the only live-provider step and is not run while writing or implementing
the deterministic tests. It requires explicit operator authorization after
QCF1–QCF18 pass.

1. Use the shipped built-in registration with no direct composition and no
   explicit operator factory. Use the existing qualified profile, Capability
   Routing Plan, environment credential boundary, Data Usage Mode, Safety
   Ceilings, completeness thresholds, and provider routes.
2. Use a newly allocated run/checkpoint namespace and retained content-addressed
   artifacts; do not clear or reuse historical reports, checkpoints, attempts,
   or IDs to manufacture a clean result.
3. Run one symbol at a time for the same latest completed mainland session:
   `601328.SS` as the bank case, then `600895.SS` as the non-bank case. Invoke
   explicit `qualified_v1_shadow` validation through the real CLI seam.
4. For each symbol, retain the safe preflight result, Capability Routing Plan
   signature, dispatcher/cache checkpoint projection, provider attempt events,
   manifest, report, and decision audit. Do not retain or print credentials or
   raw provider errors outside existing immutable provider-artifact boundaries.
5. The gate passes only if both runs avoid
   `qualified_routing_composition_missing`, complete through the registered
   production composition, route all physical calls through the coordinator,
   establish BaoStock Current Tradeability authority, show exact/non-duplicated
   attempts, and publish report plus decision audit as
   `shadow_non_authoritative` while legacy-selected output remains authoritative.
6. A typed provider/configuration failure, missing status authority, unexplained
   attempt, secret/raw-error exposure, authority drift, historical-state
   mutation, or failure of either symbol blocks the gate. A retry must use a new
   run identity and must not rewrite the failed run.
7. Before and after the gate, verify that authoritative `qualified_v1` is still
   disabled. Passing this gate produces evidence for a separate future
   activation decision; it does not enable or recommend activation.

## Out of Scope

- Enabling authoritative `qualified_v1`, changing its activation gate, or
  recommending activation.
- Changing any provider route, provider order, fallback policy, completeness
  threshold, financial eligibility rule, revision-selection rule, or
  qualification profile.
- Changing Evidence Admission, Decision Gate, Strategy Rules, Validated Decision
  Context, or the authority of legacy-selected output.
- Changing BaoStock status, lifecycle, or strict-history authority.
- Changing the daily mainland AKShare -> BaoStock -> Yahoo market route or adding
  Tushare to daily market acquisition.
- Adding speculative prewarming, parallel provider fan-out, provider probing,
  account rotation, hidden retries, or future-replay work to foreground analysis.
- Introducing a new provider cache, artifact schema, checkpoint sidecar, report
  format, ID namespace, or process-global provider registry.
- Rewriting, deleting, migrating in place, or relabeling any historical report,
  checkpoint, artifact, attempt, manifest, audit, snapshot, pin, provider row, or
  ID.
- Moving credentials into config, files, command arguments, prompts, logs, or
  persisted state.
- Running live providers during implementation or deterministic acceptance; the
  separately authorized two-symbol gate is the only live step.

## Further Notes

- ADR-0031 requires every physical call to use the centralized Provider Request
  Coordinator, with sequential fallback, shared cooldowns, exact attempt
  accounting, and no fan-out. This specification is additive and consistent
  with that decision.
- ADR-0032 continues to govern the operator’s Data Usage Mode. A shipped factory
  does not imply commercial provider entitlement.
- ADR-0033 and ADR-0034 preserve complete BaoStock Provider History Bundle and
  strict-history authority. ADR-0035 prevents the factory from adding foreground
  work for replay. ADR-0036 and ADR-0037 preserve Market Session Calendar and
  History Gap behavior.
- The highest existing seam is sufficient: host builder for static selection and
  the checkpoint-aware financial tool/dispatcher boundary for run binding. The
  factory may add lifecycle capabilities to the managed composition contract,
  but it must not add another routing or persistence authority.
- `Status: ready-for-agent` is justified because factory ownership, static
  registration and override precedence, two-phase construction, run lifecycle,
  interruption/cleanup, checkpoint/cache restoration, and shadow authority are
  all fixed above. Any implementation that cannot satisfy one of those decisions
  must return the specification to `needs-info` rather than inventing a second
  owner or weakening the boundary.

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

The completed fresh-run prototype exposed two additional Critical Ticket 06
gaps that checkpoint-seeded tests can conceal. First, a fresh financial tool
request reaches request factories that have no run-owned resolved Financial
Company Type or authoritative listing provenance and currently fail with the
typed runtime-binding error; restored dispatch can bypass this only because a
selection was already persisted. Second, six plan-declared production source
registrations terminate as `NOT_CONFIGURED` rather than invoking typed existing
production adapter seams. A complete design must originate authoritative
eligibility metadata in a fresh run and make every declared source executable
without moving policy into factories or adding another router.

## Solution

Ship and statically register one built-in qualified routing composition factory.
For an explicitly requested validation workload whose immutable Capability
Routing Plan is `qualified_v1_shadow`, the factory assembles the existing
Tushare, AKShare-Sina, Yahoo, and BaoStock adapters behind one run-owned
composition. It binds every physical request to the existing Provider Request
Coordinator, Provider Subrequest Cache, immutable artifact store, financial
dispatcher, checkpoint, and graph seams.

The factory has two deterministic construction phases under one managed
resource owner; the Financial Eligibility Acquisition Context has the separate
graph/run-host owner defined below:

1. Before graph, model, provider, database, artifact, or report work, validate
   the mode, plan, Instrument Identity, configuration, dependencies, credential
   availability, and factory contract, then create an immutable composition
   blueprint.
2. When the existing graph/dispatcher seam has the canonical run identity and
   compatible checkpoint projection, bind exactly one run runtime containing
   the coordinator authority, restored Provider Subrequest Cache, artifact
   store, provider clients/transports, adapters, router sources, and a
   provider-neutral Financial Eligibility Acquisition Context acquirer. The
   graph/run host binds exactly one immutable context before exposing financial
   request factories or a callable financial dispatcher.

For a fresh run, the authoritative construction and use sequence is:

```text
authoritative Run Asset Configuration
  -> immutable Capability Routing Plan
  -> provider-neutral eligibility metadata acquisition
  -> immutable Financial Eligibility Acquisition Context
  -> financial dispatcher and pure request factories
  -> the one capability router
  -> provider adapters
```

The resource-free blueprint and zero-I/O managed composition precede that
sequence operationally. The managed composition may assemble the one router and
its sources, but ordinary financial request factories and dispatch remain
unexposed until the run host has acquired and bound the context. The eligibility
acquirer may exercise only the plan's prerequisite metadata capabilities through
that same router, cache, coordinator, and adapters; this does not create a
bootstrap router or another provider-order authority.

For an exact compatible restoration, context and cache are restored and bound
before adapters become callable, with zero provider or model I/O. Restoration
is a separate path from fresh acquisition and never repairs missing context by
calling a live source.

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
33. As a graph/run host, I want one immutable Financial Eligibility Acquisition Context per qualified run, so that every fresh financial request shares one authoritative eligibility identity.
34. As a fresh-run user, I want company type and listing provenance acquired before ordinary financial request construction, so that a checkpoint seed is not required to make the built-in runtime callable.
35. As a policy owner, I want company type resolved once through the existing versioned resolver and qualified evidence, so that ticker, name, model prose, and factory behavior cannot invent issuer type.
36. As a lifecycle authority owner, I want BaoStock listing provenance projected through a provider-neutral metadata seam, so that request factories do not call BaoStock or duplicate its authority.
37. As a routing owner, I want prerequisite metadata acquisition and ordinary financial acquisition to use the same immutable Capability Routing Plan, so that no bootstrap route registry can diverge.
38. As a request-factory maintainer, I want factories to be pure projections of a tool request and immutable context, so that they cannot fall back, choose providers, retry, or mutate policy.
39. As a concurrent-run user, I want simultaneous first financial requests to join one context acquisition, so that company type and listing provenance are resolved once for the run.
40. As a checkpoint user, I want context identity and version bound into qualified checkpoint state, so that a resume cannot mix historical eligibility inputs with a different plan or asset.
41. As a failure-path operator, I want partial context acquisition to fail typed before callable routing escapes, so that incomplete metadata cannot create a partially usable runtime.
42. As a provider-integration maintainer, I want the six terminal `NOT_CONFIGURED` registrations replaced at the existing source-binding seam, so that every plan-declared production source is executable without a second registry.
43. As an acceptance owner, I want QCF4–QCF12 to originate context from a fresh built-in run, so that seeded runtime/checkpoint state cannot conceal a broken acquisition path.

## Implementation Decisions

### Scope and authority invariants

- This feature supplies construction, registration, lifecycle, and restoration
  only. The existing Capability Routing Plan is the sole policy authority.
- The factory must consume the plan’s exact routes and enabled capabilities. It
  must not contain a second provider-order table used for execution, silently
  fill an absent plan source, or infer policy from installed dependencies.
- Source registration is an unordered executable-binding catalog keyed by an
  already-declared `(capability, provider ID)` pair. Registration order has no
  routing meaning. The factory maps provider IDs to executable sources; the
  immutable Capability Routing Plan alone determines traversal and fallback.
- Existing financial eligibility, completeness, revision selection, Evidence
  Admission, Decision Gate, Strategy Rules, and reporting behavior are
  unchanged.
- Request factories, the composition factory, the context acquirer, and provider
  adapters must not implement fallback, provider ordering, duplicated
  completeness rules, duplicated company-type policy, another capability
  router, provider fan-out, or hidden retries. No process-global eligibility
  context or mutable provider state is permitted.
- A process-global eligibility context is explicitly prohibited; the only
  permitted context slot is owned by the graph/run host for one run.
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

The top-level factory returns one managed, plan-bound composition that owns
runtime resources. The graph/run host separately owns the one
single-assignment Financial Eligibility Acquisition Context slot for that run.
Neither ownership is split among the CLI, request factories, adapters, or global
modules, and the managed composition does not become the owner of mutable
financial eligibility state.

| Concern | Owner and required behavior |
| --- | --- |
| Immutable wiring | The built-in factory validates the plan and creates the source/request blueprint once. |
| Run binding | The composition binds one runtime only after canonical run identity and checkpoint compatibility are known. Rebinding the same run returns the same runtime; binding a different run requires a different composition execution scope. |
| Financial Eligibility Acquisition Context | The graph/run host owns exactly one single-assignment slot. A successful qualified financial run binds one immutable value; concurrent first use joins one acquisition single-flight. An identical rebind returns the same value, while a non-identical second bind fails typed. There is no merge, refresh, or process-global context. |
| Context construction | A provider-neutral acquirer constructs and validates the value from authoritative run inputs and plan-routed metadata evidence. The managed composition supplies owned resources and the one router but does not own or mutate the value and does not decide company-type or completeness policy. |
| Request-factory exposure | The composition exposes no ordinary financial request factory or callable financial dispatcher until context acquisition or exact restoration has succeeded and the run host has bound the context. |
| Coordinator authority | The bound runtime opens the existing provider-request authority and owns its store and peer connections. |
| Provider cache and artifacts | The bound runtime creates or restores one Provider Subrequest Cache and one existing immutable artifact store shared by every qualified adapter in the run. |
| Provider clients/transports | The bound runtime owns per-run Tushare, AKShare-Sina, Yahoo, and BaoStock client/transport objects. It does not set SDK module globals or create mutable factory registries. |
| Normal completion | The analysis owner closes the composition after checkpoint/report completion and before returning to post-run interaction. |
| Graph initialization/execution failure | The analysis owner interrupts and closes the composition for every `BaseException`, including `KeyboardInterrupt` and `SystemExit`, whether failure occurs before or after graph compilation. |
| Partial construction failure | A construction-local exit stack closes every resource created before the failure; no partially constructed composition is returned. |
| Partial context-acquisition failure | The context slot remains unbound, the typed original acquisition failure escapes, no partial callable routing runtime is exposed, and every already-owned resource is interrupted/closed in reverse order. Cleanup diagnostics may be retained safely but never replace the original failure. |
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

### Financial Eligibility Acquisition Context contract

Add one closed, versioned, immutable Pydantic contract named **Financial
Eligibility Acquisition Context**. It is provider-neutral authoritative run
data, not a service locator, route registry, provider cache, or error envelope.
Its deterministic identity commits to every field below using the project's
existing canonical identity rules.

| Field | Required meaning and validation |
| --- | --- |
| Contract version and context identity | A new explicit context contract version and its deterministic identity. The identity changes for any material field change and is safe to persist. |
| Run and asset bindings | Canonical run ID and authoritative Run Asset Configuration signature. These bind the value to one run/asset and are immutable acquisition metadata, not routing policy. |
| Instrument Identity | The authoritative `InstrumentIdentityEvidence` for a CNY XSHG/XSHE Equity. Symbol, venue, kind, currency, identity provenance, and asset signature must agree. |
| Run as-of date | The one authoritative analysis as-of date used for metadata and financial target construction. Tool requests must match it exactly. |
| Requested availability scope | The existing typed Financial Availability Scope requested for the run, plus the bounded `foreground_current_analysis` acquisition purpose. It cannot be broadened for prewarming or replay. |
| Financial eligibility-policy binding | The active immutable Mainland Financial Eligibility projection/binding identity. Context validation must prove it is the exact binding committed by the plan and run. |
| Routing-plan signature | The exact immutable Capability Routing Plan signature that governed prerequisite metadata acquisition and will govern ordinary financial routing. |
| Resolved Financial Company Type | The existing `FinancialCompanyTypeResolution`, including resolved type, resolution method, resolver/classifier version, canonical resolution inputs, candidate types, ambiguity/contradiction flags, and safe provenance binding to qualified evidence. A successful context cannot contain `UNKNOWN`, an ambiguous result, or an unacceptable contradiction. |
| Authoritative listing data | The authoritative listing date and existing `FinancialListingProvenance`, projected from the BaoStock issuer-lifecycle capability. Provider ID, source reference, observation time, and authority marker remain bound. |
| Canonical acquisition metadata | Only the immutable provider-neutral values needed to construct fresh requests: currency, requested consolidation scope, and canonical annual/reporting acquisition-period supersets produced by the active eligibility policy from as-of and listing data. |

The context must not contain provider fallback policy, provider ordering,
provider-specific request functions, routers or clients, mutable provider/cache
state, credentials or credential derivatives, raw provider payloads, raw
provider errors, model prose, completeness logic, retries, or provider-specific
candidate objects. Its safe company-type evidence binding may identify accepted
artifacts and canonical normalized resolver inputs, but it must not embed raw
rows or operational error text.

Context construction validates all cross-bindings before single assignment:
authoritative identity and currency, run and asset signatures, as-of date,
availability scope, canonical acquisition periods, active financial eligibility
binding, active plan signature, non-unknown resolved company type with qualified
artifact-bound evidence, and authoritative listing provenance. The identity and
safe projection include every one of these values. The host provides no field-
by-field mutation or partial bind operation.

The closed acquisition-result contract is either the complete context or a
typed failure that distinguishes at least: company-type evidence unavailable,
company type unknown/unresolved, company type ambiguous, company type
contradictory, listing metadata/provenance unavailable, context binding
mismatch, and interrupted acquisition. These distinctions are safe and payload-
free; none carries provider error text or a partial context.

### Provider-neutral company-type resolution

- Add `financial_company_type_evidence` as a prerequisite capability in the
  next version of the one Capability Routing Plan contract. Its qualified
  shadow route is `tushare -> akshare_sina -> yfinance`; the plan is immutable
  after creation, and no composition/factory table may replace or reorder that
  route.
- The capability accepts only authoritative Instrument Identity, run as-of or
  bounded evidence range, requested availability scope, and acquisition
  purpose. It does not accept a Financial Company Type, so it can run before an
  ordinary statement or indicator request exists.
- Tushare statement evidence may contribute its existing qualified `comp_type`
  declaration. AKShare-Sina statement evidence may contribute existing
  provider company-type declarations, whitelisted classifier metadata, and
  qualified financial-field signatures. Yahoo current-profile evidence may
  contribute only whitelisted provider-neutral classifier inputs and cannot
  claim a qualified provider declaration it does not possess.
- The provider-neutral acquirer projects accepted artifact-bound evidence into
  a safe Financial Company Type evidence binding and invokes the existing
  versioned `resolve_financial_company_type()` exactly once for the run/context.
  The existing `FinancialCompanyTypeResolution` remains the authority for the
  resolution method (`qualified_provider`, `classifier`, or `unresolved`),
  classifier version, canonical inputs, candidates, ambiguity, and
  contradiction.
- Company type must never be inferred from a ticker/symbol, company or display
  name, model text, prompt output, request-factory behavior, installed provider,
  provider ordering, or an adapter's incidental branch.
- A qualified retained artifact projection is checked before physical work. A
  provider request is allowed only when the current financial request needs the
  value and no exact compatible retained projection exists. Every physical call
  remains sequential, coordinated, cached, and attempt-accounted under
  ADR-0031.
- `UNKNOWN`/unresolved, ambiguous, contradictory, missing-provenance, or invalid
  resolver input produces a closed typed acquisition failure. The safe typed
  resolution outcome is preserved in that acquisition result for deterministic
  diagnosis and checkpoint-compatibility decisions, but it is never serialized
  as a successful context or partial qualified checkpoint and no callable
  financial runtime is exposed. Raw provider payloads and errors never cross
  this boundary.

This prerequisite capability breaks the bootstrap cycle explicitly:

```text
authoritative Instrument Identity + as-of + availability scope
  -> plan-routed company-type evidence
  -> safe provider-neutral evidence binding
  -> existing versioned company-type resolver
  -> immutable eligibility context
  -> ordinary financial request factory
```

An ordinary request factory is therefore never asked to discover the company
type that its own output contract already requires.

### Authoritative listing provenance

- BaoStock remains the single existing issuer-lifecycle/listing authority. The
  eligibility metadata seam uses the existing BaoStock lifecycle capability,
  including `BaoStockCapabilityAdapter.acquire_lifecycle()`, its lifecycle
  candidate listing date, and the existing authoritative Financial Listing
  Provenance projection.
- Extend the provider-neutral lifecycle response with a safe eligibility
  metadata projection that retains listing date and Financial Listing
  Provenance. The current generic capability response may not strip these values
  before context construction, and the BaoStock-specific candidate must not
  escape the adapter boundary.
- Run Asset Configuration contributes authoritative Instrument Identity but
  does not become listing-date authority. Financial rows, first observed report
  dates, ticker/name conventions, or model text cannot be used to infer listing
  date.
- Request factories and dispatchers do not call BaoStock directly. They consume
  only the bound provider-neutral context. No second lifecycle source,
  replay-only call, prewarm call, or duplicate BaoStock authority is introduced.
- A compatible retained lifecycle projection may satisfy acquisition. Otherwise
  the current target construction need uses the existing plan's
  `issuer_lifecycle -> baostock` route through the same router, cache, and
  coordinator. No additional provider work is performed merely to improve a
  future restore.

### Fresh-run acquisition and pure request factories

Blueprint validation and managed-runtime assembly remain provider-I/O-free. On
the first current qualified financial request in a fresh
`qualified_v1_shadow` run, the graph/run host starts one context-acquisition
single-flight. Concurrent callers join it. The acquirer first uses exact
compatible retained metadata, then only the prerequisite plan routes required
for current work. On success the host binds the immutable context once and the
managed composition may expose financial factories and the shadow dispatcher.

The fresh path must work without seeded runtime or checkpoint state:

```text
resource-free blueprint
  -> zero-I/O managed composition
  -> provider-neutral eligibility acquisition
  -> run-host single-assignment context
  -> pure request factories
  -> shadow dispatcher
  -> the one plan-bound capability router
  -> production provider adapters
```

Statement and indicator request factories have the pure conceptual interface
`(FinancialToolRequest, Financial Eligibility Acquisition Context) -> typed
routing request`. They validate binding equality and project context values;
they receive no provider clients, source catalog, router, coordinator, mutable
state, fallback policy, or completeness policy. They may not resolve company
type, acquire metadata, choose a provider, perform fallback, retry, alter the
eligibility or completeness policy, rebuild acquisition periods, or mutate
listing provenance.

Version the statement and indicator routing-request contracts so both retain
the complete Financial Company Type resolution and its safe artifact/provider
evidence binding. The existing statement request's resolved enum alone and the
existing indicator request's resolution without artifact evidence are not
sufficient. Listing date/provenance, availability scope, as-of, and eligibility
binding must likewise remain bound through request, routing, selection, and
checkpoint evidence.

If acquisition is interrupted or any required metadata is unavailable,
ambiguous, contradictory, invalid, or mismatched, failure is typed and
secret-free. The context slot stays unbound; no request factory, dispatcher, or
partial callable router escapes; lifecycle cleanup remains reverse-order and
idempotent; and the original acquisition failure remains primary over any
cleanup failure.

### Adapter and route composition

The factory supplies source functions for the provider IDs already named by the
Capability Routing Plan. The router iterates the plan; the factory does not
reorder it. The following is the exact provider-ID-to-production-source coverage
matrix. It is a binding matrix, not a second route registry; only provider IDs
already declared for a capability in the immutable plan are registered, and
table/registration order has no execution meaning.

| Plan capability | Provider ID | Required executable production-source binding |
| --- | --- | --- |
| Balance sheet | `tushare` | Existing Tushare statement adapter `acquire_balance_sheet()` over Tushare `balancesheet`. |
| Income statement | `tushare` | Existing Tushare statement adapter `acquire_income_statement()` over Tushare `income`. |
| Cash flow | `tushare` | Existing Tushare statement adapter `acquire_cash_flow()` over Tushare `cashflow`. |
| Balance sheet, income statement, cash flow | `akshare_sina` | Existing AKShare-Sina statement adapter over `stock_financial_report_sina` with the requested statement symbol. |
| Balance sheet, income statement, cash flow | `yfinance` | Typed Yahoo statement adapter over the run-owned Yahoo facade's `Ticker` annual and quarterly `balance_sheet`, `income_stmt`, and `cashflow` surfaces. This replaces the `yfinance / financial_statements` terminal registration. |
| Financial indicators | `tushare` | Existing Tushare financial-indicator adapter `acquire_financial_indicators()` over Tushare `fina_indicator`. |
| Financial indicators | `akshare` | Typed AKShare indicator adapter over the run-owned AKShare facade's `stock_financial_analysis_indicator`. This replaces `akshare / financial_indicators`. |
| Financial indicators | `baostock_qualified_families` | Existing BaoStock ratio-family adapter over `query_profit_data`, `query_operation_data`, `query_growth_data`, `query_balance_data`, `query_cash_flow_data`, and `query_dupont_data`. |
| Financial indicators | `yfinance` | Typed Yahoo current-profile adapter over the run-owned Yahoo facade's `Ticker.get_info()` or `Ticker.info`, projected only as current-profile/classifier evidence. This replaces `yfinance / current_financial_profile`. |
| Adjustment factors | `baostock` | Existing BaoStock capability/bundle adapter `acquire_provider_history_bundle()` over `query_history_k_data_plus`, `query_adjust_factor`, and the status query, preserving complete bundle semantics. |
| Adjustment factors | `tushare` | Existing Tushare adjustment-factor adapter `acquire_adjustment_factors()` over Tushare `adj_factor`. |
| Adjustment factors | `akshare` | Typed AKShare factor adapter over the run-owned AKShare facade's `stock_zh_a_daily(adjust="qfq-factor")`. This replaces `akshare / adjustment_factors`. |
| Adjustment factors | `yfinance_derived` | Typed Yahoo-derived factor adapter over the run-owned Yahoo facade's `Ticker.history(auto_adjust=False)`, deriving `Adj Close / Close` and always marking output `derived=True` and `degraded=True`. This replaces `yfinance_derived / derived_adjustment_factors`. |
| Suspension status / Current Tradeability | `baostock` | Existing BaoStock capability adapter `acquire_session_status()` over `query_history_k_data_plus(... tradestatus,isST ...)`; BaoStock alone establishes Current Tradeability. |
| Issuer name events | `tushare` | Existing Tushare name-event adapter `acquire_name_events()` over Tushare `namechange`. |
| Issuer name events | `akshare` | Typed AKShare name-event adapter over the run-owned AKShare facade's `stock_info_change_name`. This replaces `akshare / issuer_name_events`. |
| Issuer lifecycle | `baostock` | Existing BaoStock capability adapter `acquire_lifecycle()` over `query_stock_basic`, extended only with the provider-neutral listing metadata projection required by the context. |
| Financial company-type evidence | `tushare` | Safe evidence projection from the existing Tushare statement acquisition/normalization seam, including only a qualified `comp_type` declaration and artifact-bound resolver inputs. |
| Financial company-type evidence | `akshare_sina` | Safe evidence projection from the existing AKShare-Sina statement acquisition/normalization seam, including qualified declarations and whitelisted classifier inputs/signatures. |
| Financial company-type evidence | `yfinance` | Safe classifier-input projection from the typed Yahoo current-profile binding; it cannot claim strict PIT or qualified declaration authority. |

The six exact production registrations currently returning an unavailable
`NOT_CONFIGURED` response and `PhysicalAttemptNotMade` are therefore replaced,
not supplemented: one Yahoo `financial_statements` registration covering all
three statements, AKShare `financial_indicators`, Yahoo
`current_financial_profile`, AKShare `adjustment_factors`, Yahoo-derived
`derived_adjustment_factors`, and AKShare `issuer_name_events`. No terminal stub
may remain for a plan-declared qualified production pair.

Each replacement is a typed adapter/normalizer installed through the existing
production source-registration to bound-source seam. It uses the already
run-owned provider client/module facade, the one Provider Subrequest Cache, and
the one Provider Request Coordinator. `akshare` shares the physical
`akshare_sina` upstream identity, `yfinance_derived` shares the physical
`yfinance` identity, and `baostock_qualified_families` shares the physical
`baostock` identity. These mappings preserve shared capacity, cooldown, cache,
and ADR-0031 physical-attempt accounting.

Typed Yahoo sources call the run-owned Yahoo facade's `Ticker` surface inside
the existing bound-source/cache/coordinator boundary. They must not wrap legacy
Yahoo helper acquisition paths that open their own coordination/store boundary;
nested coordination would change cache semantics and physical-attempt identity.

`daily_market_snapshot` still names `akshare`, `baostock`, and `yfinance`, but it
is intentionally excluded from this qualified financial source assembler. Its
AKShare -> BaoStock -> Yahoo route remains owned by the existing market-history
composition.

Adapter result conversion uses the existing provider-response, manifest,
selection, completeness, and capability-response contracts. No raw frame or SDK
object crosses the adapter boundary. Factor and name calls share the run cache
but retain distinct endpoint scopes, subrequest keys, artifacts, outcomes, and
authority.

BaoStock transport ownership is per run. Login/session creation is lazy and
serialized inside the run-owned transport; logout happens on terminal request
failure, interruption, partial construction failure, and close. Tushare uses a
per-run API client, AKShare-Sina calls are made through a run-owned facade, and
Yahoo creates request-local objects/sessions through the run-owned facade and
existing coordinated transport path. The factory creates no mutable module
singleton.

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
  ledgers, context identity/version, and checkpoint compatibility before
  dispatch.
- For `qualified_v1_shadow`, the authoritative dispatcher remains legacy and
  receives no qualified router. A separate shadow dispatcher receives the one
  bound qualified router, pure request factories, immutable context, and single
  restored Provider Subrequest Cache only after all bindings validate.
- The existing shadow financial dispatch ledger remains the owner of qualified
  selection progress and Provider Subrequest Cache checkpoint. Its existing
  qualified checkpoint projection is versioned to carry the safe Financial
  Eligibility Acquisition Context projection; no second checkpoint file,
  sidecar, or competing checkpoint owner is introduced.
- The checkpoint binds the context contract version, deterministic context
  identity, complete safe context fields, run/asset signatures, authoritative
  Instrument Identity, run as-of date, requested availability scope, financial
  eligibility-policy binding, routing-plan signature, company-type resolution
  and evidence identity, listing date/provenance, and canonical acquisition
  metadata. Context identity therefore cannot be detached from the cache,
  dispatcher selection state, or historical request identities it governed.
- On exact compatible resume, validate every context binding together with run
  scope, provider-chain topology, immutable artifacts, attempt identities,
  Provider Subrequest Cache, and dispatcher state before any adapter, request
  factory, or dispatcher becomes callable. Restore and single-assign the
  immutable context, restore terminal cache entries from existing immutable
  artifact references, then restore dispatcher results. The exact repeated
  request returns identical context, cache, attempts, candidates, selections,
  artifacts, and IDs with zero provider/network I/O, zero coordinator permits,
  zero new attempt events, and zero model I/O. Local checkpoint/artifact reads
  are the restoration boundary.
- A missing context projection, unknown/ambiguous/contradictory company-type
  result, non-authoritative or mismatched listing provenance, context identity or
  version mismatch, malformed context, plan/asset/as-of/availability/eligibility
  mismatch, or corrupt cache/artifact projection fails typed before external
  work. Restoration does not call the provider-neutral acquirer, silently
  reconstruct context from live providers, delete bad state, reuse a convenient
  subset, or rewrite the historical checkpoint.
- Interrupted in-flight entries are never serialized as available. Checkpointing
  occurs only at the existing safe terminal boundary; otherwise the existing
  unsafe-checkpoint failure is preserved.
- Legacy checkpoints remain readable by the legacy path. Pre-v2 or mismatched
  qualified checkpoints follow the existing explicit start-new/fail-typed
  compatibility rules. Choosing start-new requires a new run identity and the
  fresh acquisition path; it is never an implicit repair of a restoration. No
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
  financial tool node, graph/run-host context slot, provider-neutral metadata
  acquirer, pure request factories, dispatcher, qualified router, adapters,
  Provider Subrequest Cache, and Provider Request Coordinator. This is the
  highest seam that proves fresh ownership without exposing implementation
  order as the assertion target.
- Adapter-specific tests remain prior art for payload normalization and typed
  outcomes. Routing-selection tests remain prior art for sequential salvage and
  authority. Provider coordinator/cache tests remain prior art for attempts,
  single-flight, and restoration. Rollout, checkpoint, report, decision-audit,
  and corrected-shadow joint acceptance remain prior art for shadow authority
  and compatibility.
- Contract tests cover the closed context/evidence/checkpoint projections,
  cross-binding validation, deterministic identity, typed ambiguous/unknown/
  contradictory outcomes, pure request-factory projection, and rejection of
  forbidden provider-specific or mutable fields. Lifecycle tests inject failure
  at every metadata-acquisition and context-bind boundary.
- The real two-symbol gate is deliberately separate from deterministic
  acceptance. It runs only after all non-live criteria pass and only with
  explicit operator authorization.

### Deterministic acceptance criteria

QCF1–QCF18 retain their existing IDs, scenarios, authority, and pass/fail
meaning. The following fresh-origin condition strengthens, but does not waive or
replace, QCF4–QCF12:

- Every QCF4–QCF11 execution starts from the statically registered built-in,
  resource-free blueprint, and zero-I/O managed composition with no manually
  seeded eligibility context, callable routing runtime, dispatcher selection,
  or checkpoint. It must originate one context through the provider-neutral
  acquisition seam, resolve company type once, bind authoritative BaoStock
  listing provenance, and single-assign the resulting identity before ordinary
  request-factory use. Any restore control in these matrices is first produced
  by such a fresh execution rather than hand-authored state.
- QCF12's source checkpoint must itself be produced by that same fresh built-in
  path. Only the second phase is restoration; it must bind the exact context and
  cache with zero provider/model I/O. A manually seeded context or checkpoint
  cannot satisfy QCF12.
- Prerequisite metadata calls are recorded as distinct coordinated physical
  attempts under their plan capabilities. The original ordinary-route attempt
  assertions still apply after context binding; context acquisition cannot be
  hidden inside an ordinary provider attempt or omitted from ADR-0031 counts.

| ID | Scenario | Required result |
| --- | --- | --- |
| QCF1 | Start CLI and programmatic `qualified_v1_shadow` validation with qualified capabilities enabled, no injected composition, and no explicit factory path. | The statically registered built-in factory is selected, the plan-bound managed composition reaches the graph, and `qualified_routing_composition_missing` is absent. No test module or dynamic-import fixture participates. |
| QCF2 | Supply a direct composition, then separately an explicit operator factory. | Direct composition wins without invoking either factory; explicit operator factory wins over the built-in; the existing safe-config and plan-signature validations remain exact. |
| QCF3 | Run legacy mode, ordinary non-validation shadow mode, an empty/non-applicable qualified plan, and attempted `qualified_v1`. | Legacy/non-validation paths construct no qualified resource and perform zero qualified I/O; `qualified_v1` fails with the existing authority-disabled preflight before the built-in factory is called. |
| QCF4 | From the required fresh origin, return complete fixed Tushare company-type, statement, and indicator fixtures plus authoritative BaoStock lifecycle metadata for all eligible targets. | The run originates and binds its context before ordinary dispatch. Tushare is selected where the plan names it first; all expected annual/reporting periods and indicator families are retained and selected; no lower ordinary financial provider is called; physical attempts equal the exact prerequisite metadata calls plus Tushare endpoint calls. |
| QCF5 | From the required fresh origin, make one Tushare endpoint unavailable and another return usable rows plus partial eligible-period/family gaps. | One context is originated without a seed; successful Tushare artifacts and periods survive; only unresolved statement periods or indicator families continue; typed gaps and all prerequisite/ordinary attempts are exact; no successful bulk request is repeated per target. |
| QCF6 | From the required fresh origin, leave statement gaps after Tushare and make AKShare-Sina return usable periods, including partial salvage across its statement types. | Context acquisition resolves company type once before factory use; AKShare-Sina is called sequentially only for unresolved ordinary work, its usable artifacts/periods are retained, and Yahoo is not called for resolved targets. |
| QCF7 | From the required fresh origin, leave eligible statement or indicator gaps after higher providers and provide only Yahoo fallback data. | The context is self-originated first; Yahoo is last, coordinated, and selected only under existing degraded/current-only contracts; candidate-free current profile and Yahoo-derived factor behavior remain exact; no Yahoo evidence becomes strict PIT or native factor authority. |
| QCF8 | From the required fresh origin, exercise BaoStock raw/factor/status/lifecycle and ratio-family fixtures with tradeable and suspended latest sessions. | The context carries the BaoStock lifecycle listing date and authoritative provenance before factory use; BaoStock alone establishes Current Tradeability; strict bundle identity requires its complete three-component binding; missing ratio families are requested narrowly; Tushare names and other sources cannot establish status. |
| QCF9 | From the required fresh origin, exercise Adjustment Factors and Name Events together, with Tushare factor success, Tushare name success, BaoStock factor success/failure, and Yahoo-derived terminal fallback. | The context is acquired once without seeded state; factor and name requests use distinct plan routes, endpoint scopes, keys, artifacts, and attempts while sharing one cache; name output remains descriptive; Yahoo factor output is `derived` and `degraded`; provider bundles are never blended. |
| QCF10 | From the required fresh origin, run complete, retryable-failure, terminal-failure, cooldown-skip, partial-fallback, and fresh-produced restored-cache matrices for every assembled provider. | Context and provider work share the one plan/router/coordinator. Every actual transport call has one and only one attempt event; retries and fallback match exact configured cardinality; cache hits, skips, normalization, selection, and restore add zero attempts; no hidden adapter retry or fan-out exists. |
| QCF11 | From the required fresh origin, concurrently issue identical first requests and request multiple periods/families/graph calls covered by one bulk artifact. | Concurrent first use performs one company-type resolution, one compatible listing acquisition, and one context bind; one leader performs each physical sequence, followers receive the same terminal result, bulk payloads are acquired once, reuse counts are deterministic, and differing material keys do not collide. |
| QCF12 | Produce a checkpoint from a fresh built-in context acquisition and terminal qualified shadow request, close all resources, reconstruct through the built-in factory, and repeat the exact request. | Context version/identity and all context fields, plan, eligibility binding, dispatcher state, cache entries, artifacts, candidates, selections, attempts, and IDs restore exactly; the repeat causes zero provider/network/model I/O, zero coordinator permits, and zero new attempts. Missing, contradictory, corrupt, or mismatched context/cache controls fail typed before work and do not call live providers or rewrite history. |
| QCF13 | Supply missing token/dependency, invalid mode, invalid plan/asset/context signature, invalid pacing/Safety Ceiling/account scope/Data Usage Mode/runtime root, incomplete source coverage including any of the six required executable bindings, and unsafe factory output. | Each pre-construction failure is secret-free and occurs before graph/model/provider work, store/artifact/report creation, or physical attempts. Typed context-acquisition failures occur only after allowed prerequisite work, leave the context slot unbound, and expose no partial callable composition. |
| QCF14 | Inject failure after each construction step, at every eligibility-metadata acquisition and context-binding step, during graph initialization, during provider dispatch, during graph execution, during checkpoint publication, and interrupt with `KeyboardInterrupt`. | The original failure remains primary; no partial context or callable dispatcher escapes; interrupt/close occur exactly once; new work stops; all sessions, BaoStock login, coordinator connections/store, leases, and workers are released or terminally abandoned; repeated close is harmless; no process-global mutable provider or eligibility state remains. |
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
   signature, Financial Eligibility Acquisition Context identity/projection,
   dispatcher/cache checkpoint projection, provider attempt events, manifest,
   report, and decision audit. Do not retain or print credentials or raw
   provider errors outside existing immutable provider-artifact boundaries.
5. The gate passes only if both runs avoid
   `qualified_routing_composition_missing`, complete through the registered
   production composition, originate and bind one eligibility context without
   seeded state, route all physical calls through the coordinator, establish
   BaoStock Current Tradeability authority, show exact/non-duplicated attempts,
   and publish report plus decision audit as
   `shadow_non_authoritative` while legacy-selected output remains authoritative.
6. A typed provider/configuration failure, missing status authority, unexplained
   attempt, secret/raw-error exposure, authority drift, historical-state
   mutation, or failure of either symbol blocks the gate. A retry must use a new
   run identity and must not rewrite the failed run.
7. Before and after the gate, verify that authoritative `qualified_v1` is still
   disabled. Passing this gate produces evidence for a separate future
   activation decision; it does not enable or recommend activation.

## Implementation Work Areas and Dependencies

The existing Tickets 01–05 remain valid for the scopes they already define, but
they do not by themselves close the fresh-run context and executable-source
gaps. Add the following bounded implementation work areas; prototype code is
design evidence only and is not copied into production.

| Work area | Exact scope | Blocked by | Blocks |
| --- | --- | --- | --- |
| **02A — Provider-neutral eligibility acquisition context contracts** | Version the Capability Routing Plan with the prerequisite company-type-evidence capability; add the closed immutable context, safe company-type evidence binding, context identity/projection, typed acquisition failures, and cross-binding validators. No provider implementation or live I/O. | Existing **01**. | 02B, 02C, 02D, 04A, 05A, and Ticket 06. |
| **02B — Fresh company-type and listing-provenance acquisition** | Implement the provider-neutral acquirer against injected plan/router sources; use the existing versioned company-type resolver once; project BaoStock lifecycle listing data through the provider-neutral response; reuse exact retained metadata; fail closed on unknown, ambiguity, contradiction, missing provenance, or mismatch. | 02A and existing **02**. | 04A, 05A, and Ticket 06. |
| **02C — Pure request-factory integration with the immutable context** | Version statement and indicator requests to retain resolution/evidence/listing/eligibility bindings; replace failing fresh factories with pure two-input projections; prohibit provider, fallback, completeness, inference, and mutation behavior in factories. | 02A. | 04A, 05A, and Ticket 06. |
| **02D — Executable provider-source wiring for the six terminal registrations** | Replace exactly the Yahoo statements, AKShare indicators, Yahoo current profile, AKShare factors, Yahoo-derived factors, and AKShare name-event `NOT_CONFIGURED` registrations with the typed bindings in the exact matrix above; add safe company-type evidence projections; preserve shared physical-provider/coordinator/cache identities. | 02A and existing **02**. | 04A and Ticket 06. |
| **04A — Managed lifecycle integration for context acquisition and binding** | Give the graph/run host the single-assignment slot and single-flight; bind acquired context before exposing factories/dispatcher; integrate interruption, partial-failure cleanup, idempotence, and original-failure precedence through the existing managed composition. | 02A, 02B, 02C, 02D, and existing **04**. | 05A and Ticket 06. |
| **05A — Checkpoint context binding and restoration compatibility** | Version the existing qualified checkpoint projection with context identity/version and safe fields; validate and bind context before cache/dispatcher exposure; prove exact zero-I/O restore; fail typed without live repair or historical rewrite. | 02A, 02B, 02C, 04A, and existing **05**. | Ticket 06. |

The exact dependency graph is:

```text
01 -> 02, 03, 04, 05, 02A
02A + 02 -> 02B
02A -> 02C
02A + 02 -> 02D
02A + 02B + 02C + 02D + 04 -> 04A
02A + 02B + 02C + 04A + 05 -> 05A
03 + 02D + 04A + 05A -> 06
06 -> 07
```

Existing Ticket 03 remains the static-selection/caller-precedence slice and may
proceed after Ticket 01; it does not own context construction. Existing Ticket
04 remains the resource-lifecycle prerequisite to 04A. Existing Ticket 05
remains the cache/dispatcher restoration prerequisite to 05A. Ticket 06 remains
blocked even if its original Tickets 02–05 are marked complete: it cannot resume
until 02A, 02B, 02C, 02D, 04A, and 05A are complete and their deterministic
tests pass. Ticket 07 remains blocked by Ticket 06 and fresh explicit operator
authorization for live-provider work.

## Out of Scope

- Enabling authoritative `qualified_v1`, changing its activation gate, or
  recommending activation.
- Changing any existing ordinary provider route/order, fallback policy,
  completeness threshold, financial eligibility rule, revision-selection rule,
  or qualification profile. The new versioned prerequisite company-type-
  evidence capability is the sole additive route defined by this amendment.
- Changing Evidence Admission, Decision Gate, Strategy Rules, Validated Decision
  Context, or the authority of legacy-selected output.
- Changing BaoStock status, lifecycle, or strict-history authority.
- Changing the daily mainland AKShare -> BaoStock -> Yahoo market route or adding
  Tushare to daily market acquisition.
- Adding speculative prewarming, parallel provider fan-out, provider probing,
  account rotation, hidden retries, or future-replay work to foreground analysis.
- Introducing a new provider cache, artifact schema, checkpoint sidecar, report
  format, ID namespace, process-global provider registry, or process-global
  eligibility context.
- Rewriting, deleting, migrating in place, or relabeling any historical report,
  checkpoint, artifact, attempt, manifest, audit, snapshot, pin, provider row, or
  ID.
- Moving credentials into config, files, command arguments, prompts, logs, or
  persisted state.
- Running live providers during implementation or deterministic acceptance; the
  separately authorized two-symbol gate is the only live step.
- Copying the fresh-run prototype into production, importing it at runtime, or
  treating its code/tests as implementation completion. Its report is
  authoritative design evidence only; production work uses existing contracts
  and seams named in this specification.

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
- The highest seams are fixed: host builder for static selection; graph/run host
  for context lifetime and single assignment; provider-neutral acquisition over
  the one plan/router for metadata construction; and the checkpoint-aware
  financial tool/dispatcher boundary for request binding/restoration. The
  factory may add lifecycle capabilities to the managed composition contract,
  but it must not add another routing, eligibility, or persistence authority.
- `Status: ready-for-agent` is justified because fresh-run ownership, context
  fields and identity, provider-neutral company-type and BaoStock listing
  acquisition, pure request factories, the exact provider-source wiring matrix,
  partial-failure lifecycle behavior, checkpoint context restoration, ticket
  dependencies, and shadow/legacy authority are fully resolved above. Ticket 06
  is nevertheless implementation-blocked until 02A–02D, 04A, and 05A are
  complete. Any implementation that cannot satisfy one of these decisions must
  return the specification to `needs-info` rather than inventing a second owner
  or weakening the boundary.

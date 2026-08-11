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

The completed QCF4 metadata-compatibility prototype exposed a third Critical
gap: QCF4 currently equates complete normalized Tushare indicator values with
compatible filing metadata and therefore requires those candidates to be
selected. That implication is false. Instrument Identity currency, normalized
value unit, financial value currency applicability, and filing-consolidation
scope are separate semantic dimensions. A dimensionless ratio can be complete
and correctly associated with a CNY Instrument Identity while value currency is
semantically `NOT_APPLICABLE`; the same candidate can still be incompatible
because its material filing-consolidation scope is `UNPROVEN`. `UNKNOWN` is
neither proof nor semantic irrelevance. Ticket 06 therefore remains
FAIL/BLOCKED until the provider-neutral metadata contract and corrected QCF4
path are implemented.

The completed Ticket 07 temporal-eligibility prototype exposed a fourth
Critical gap in the fresh metadata path. The failed `601328.SS` run requested
mainland session `2026-08-10`, while its Tushare company-type artifact was
retrieved at `2026-08-10T16:48:07Z`, or `2026-08-11 00:48:07` in
`Asia/Shanghai`. The retained and post-route validators require the local
retrieval calendar date to equal the requested session date, so they rejected
the artifact even though `2026-08-10` was still the latest completed mainland
session. Local midnight is not a Market Session Calendar boundary and does not
prove when the evidence became effective or available. The failed run and all
historical state remain immutable, Ticket 07 remains **BLOCKED**, and
authoritative `qualified_v1` remains disabled.

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

For financial indicators, use Design A's provider-neutral semantic metadata
model with Design C's existing fallback behavior. Every retained normalized
field is classified by versioned policy before compatibility is assessed. A
metadata-incompatible candidate remains retained but cannot satisfy selection;
only the unresolved family continues sequentially through the immutable
Capability Routing Plan. No provider-specific router exception, requested-
metadata backfill, companion-filing inference, completeness-threshold change,
or Evidence Admission rewrite is introduced.

For fresh and retained financial eligibility metadata, use temporal Design D:
keep provider observation/retrieval time separate from provider-derived
effective/publication proof. For `current_analysis_only`, use Design B's
session-bounded frontier: the requested mainland session remains eligible only
while it equals `latest_completed_session(...)` under the pinned Market Session
Calendar at one run-owned `eligibility_evaluated_at`. The frontier advances when
the next open market session completes, not at local midnight, next-session
open, or an elapsed grace window. `strict_historical_pit` remains a separate,
stricter exact-cutoff predicate and never inherits the current-analysis window.

One versioned, provider-neutral temporal evaluator owns this decision for
fresh and retained company-type and listing/lifecycle metadata, context
construction, request binding, checkpoint validation, and replay. It consumes
immutable provider proof and a pinned calendar binding, produces one immutable
typed context-level decision binding (with its artifact subdecisions), and is
evaluated in one transaction once inside the existing run-owned
single-flight with an injected fixed `eligibility_evaluated_at`. Exact replay
uses the recorded decision inputs and result with zero provider/model I/O; it
does not consult today's calendar frontier or reinterpret historical evidence.

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
44. As an evidence consumer, I want Instrument Identity currency separated from financial value currency and normalized unit, so that issuer identity is not mistaken for provider value evidence.
45. As a compatibility-policy owner, I want `PROVEN`, `UNPROVEN`, and `NOT_APPLICABLE` to be closed assessment states, so that missing proof cannot be confused with semantic irrelevance.
46. As an audit reader, I want a provider's raw `UNKNOWN` or `XXX` marker retained separately from semantic assessment state, so that unknown metadata never becomes compatible proof.
47. As an indicator maintainer, I want every normalized family-field pair classified by versioned policy data, so that applicability never depends on whichever values happen to be populated in one response.
48. As a ratio user, I want value currency declared `NOT_APPLICABLE` for dimensionless ratios and rates, so that `XXX` does not falsely conflict with the CNY Instrument Identity.
49. As a monetary or per-share user, I want currency and unit proof to remain mandatory, so that missing or request-derived currency fails closed or degrades under the existing contract.
50. As a filing-scope user, I want every current statement-derived indicator family to require proven consolidation scope, so that `UNKNOWN` cannot be converted to `CONSOLIDATED`.
51. As a routing owner, I want complete-but-metadata-incompatible Tushare candidates retained and rejected while only unresolved families fall through, so that Design C behavior remains plan-owned and deterministic.
52. As a fallback provider maintainer, I want AKShare, BaoStock, and Yahoo judged only by their own truthful metadata contracts, so that one provider's weakness neither disqualifies nor qualifies another.
53. As a lineage owner, I want companion-filing metadata rejected without an exact stable cross-artifact filing identity, so that statement scope or currency is never inferred onto an indicator artifact.
54. As an Evidence Admission owner, I want already-normalized applicability delivered at the existing trust boundary, so that material `UNPROVEN` metadata is rejected and explicit `NOT_APPLICABLE` dimensions alone are ignored.
55. As a strict-replay user, I want missing `f_ann_date` to remain current-only at most, so that metadata applicability cannot bypass first-publication or observation requirements.
56. As a checkpoint user, I want new semantic-policy and compatibility identities restored exactly, so that resume does not reinterpret old candidates under a new policy.
57. As a historical-artifact reader, I want recorded old versions replayed under their pinned contracts and never rewritten, so that the amendment is forward-versioned rather than migratory.
58. As a QCF4 owner, I want complete Tushare statements to stop statement fallback while incompatible indicator families continue, so that acceptance proves metadata truth rather than provider preference.
59. As a regression owner, I want the separately completed deterministic live-test isolation fix retained, so that deterministic acceptance remains isolated from live providers independently of this metadata change.
60. As a current-analysis user, I want metadata retrieved after local midnight to remain eligible while my requested session is still the latest completed mainland session, so that calendar dates do not create false rejections.
61. As a Market Session Calendar owner, I want the current-analysis frontier to advance only when the next open session completes, so that weekends, holidays, and intraday retrieval use market semantics.
62. As an evidence owner, I want `requested_session`, `evidence_effective_as_of`, `ann_date`, `f_ann_date`, `observed_at`, and `retrieved_at` kept separate, so that request intent cannot masquerade as provider proof.
63. As an audit owner, I want observation and retrieval timestamps preserved immutably, so that late retrieval is visible and never rewritten to manufacture eligibility.
64. As a provider-contract owner, I want `evidence_effective_as_of` populated only by an explicit provider/evidence-class derivation, so that ticker, company name, route, request defaults, retrieval date, and model output cannot fabricate applicability.
65. As a current-analysis user, I want retrieval immediately before and during the next mainland session to remain eligible for the prior session until that later session closes, so that the frontier reflects completed evidence rather than session open.
66. As a weekend or holiday user, I want the prior open session to remain the frontier throughout non-session dates, so that no arbitrary wall-clock grace window is needed.
67. As a strict-replay user, I want post-cutoff retrieval rejected unless authoritative proof binds the exact revision to pre-cutoff availability, so that current-frontier eligibility cannot weaken no-lookahead guarantees.
68. As a strict-replay user, I want missing `f_ann_date` or exact first-publication/revision availability proof to fail closed, so that an old effective date cannot prove historical availability.
69. As an integration maintainer, I want retained metadata, fresh provider metadata, context construction, request binding, checkpoints, and replay to call one temporal evaluator, so that equivalent equality checks cannot diverge.
70. As a checkpoint user, I want both accepted and rejected temporal decisions persisted with their pinned calendar and evaluation inputs, so that exact restore reproduces the original result without live repair.
71. As a historical-state owner, I want old plan/context checkpoints interpreted only under their recorded temporal contracts, so that a new policy never relabels the failed Ticket 07 evidence.
72. As a new-plan user with an old checkpoint, I want a typed incompatibility or explicit `START_NEW`, so that missing temporal proof is never invented by migration.
73. As a concurrency user, I want simultaneous first metadata requests to share one fixed evaluation time and one terminal temporal decision, so that a midnight or session-close race cannot split the run.
74. As a rollout owner, I want all temporal matrices and the complete QCF1–QCF18, PE1–PE15, and AC1–AC13 suites rerun before Ticket 07, so that the policy change receives full deterministic acceptance.
75. As a rollout owner, I want any Ticket 07 retry to allocate a new run ID, run `601328.SS` first, and run `600895.SS` only after the bank case passes, so that the immutable failed run is not reused and the gate remains fail-closed.

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
- Existing financial-period eligibility v2, completeness thresholds/math,
  revision selection, Evidence Admission trust boundary, Decision Gate,
  Strategy Rules, and decision authority are unchanged. Indicator metadata
  compatibility is an additional provider-neutral pre-selection hard gate and
  receives the exact forward versions defined below.
- Temporal Design D is authoritative for new metadata decisions, with Design
  B's session-bounded frontier used only for `current_analysis_only`. Exact
  local-calendar equality, arbitrary next-day grace windows, and ad hoc `<=` or
  `>=` substitutions are not authorities. `strict_historical_pit` retains its
  separate exact-cutoff proof and is never weakened by the current frontier.
- The pinned mainland Market Session Calendar remains the sole session/frontier
  authority under ADR-0036. Providers prove evidence applicability and
  publication; the calendar proves which session is latest completed. Neither
  authority may substitute for the other.
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
| Requested session and temporal mode | `requested_session` is the one authoritative open mainland analysis session used for metadata and financial target construction. It is request intent, never provider proof. The context also binds `current_analysis_only` or `strict_historical_pit`; tool requests must match both exactly. Historical v1 `as_of_date` remains readable only under its recorded contract. |
| Temporal evaluation binding | Context v2 binds `provider-neutral-temporal-eligibility-v1`, `market-session-eligibility-binding-v1`, the pinned calendar revision, the injected fixed `eligibility_evaluated_at`, the latest-completed-session decision, and the accepted temporal-decision identities for company-type and listing/lifecycle evidence. Strict mode additionally binds `strict_cutoff` and any exact-revision availability proof. |
| Requested availability scope | The existing typed Financial Availability Scope requested for the run, plus the bounded `foreground_current_analysis` acquisition purpose. It cannot be broadened for prewarming or replay. |
| Financial eligibility-policy binding | The active immutable Mainland Financial Eligibility projection/binding identity. Context validation must prove it is the exact binding committed by the plan and run. |
| Routing-plan signature | The exact immutable Capability Routing Plan signature that governed prerequisite metadata acquisition and will govern ordinary financial routing. |
| Resolved Financial Company Type | The existing `FinancialCompanyTypeResolution`, including resolved type, resolution method, resolver/classifier version, canonical resolution inputs, candidate types, ambiguity/contradiction flags, and safe provenance binding to qualified evidence. A successful context cannot contain `UNKNOWN`, an ambiguous result, or an unacceptable contradiction. |
| Authoritative listing data | The authoritative listing date and existing `FinancialListingProvenance`, projected from the BaoStock issuer-lifecycle capability. Provider ID, source reference, observation time, and authority marker remain bound. |
| Canonical acquisition metadata | Only the immutable provider-neutral values needed to construct fresh requests: requested currency, requested consolidation scope, and canonical annual/reporting acquisition-period supersets produced by the active eligibility policy from as-of and listing data. Currency and scope are request intent; currency also binds authoritative Instrument Identity. Neither is provider evidence. |

The context must not contain provider fallback policy, provider ordering,
provider-specific request functions, routers or clients, mutable provider/cache
state, credentials or credential derivatives, raw provider payloads, raw
provider errors, model prose, completeness logic, retries, or provider-specific
candidate objects. Its safe company-type evidence binding may identify accepted
artifacts and canonical normalized resolver inputs, but it must not embed raw
rows or operational error text.

Context construction validates all cross-bindings before single assignment:
authoritative identity and currency, run and asset signatures, requested
session/mode and temporal decisions,
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

### Provider-neutral temporal terminology and evidence contract

The completed temporal-eligibility prototype report is authoritative design
evidence. New writes use the following closed terms exactly; historical fields
remain readable under their recorded contracts and are not renamed in place.

| Term | Normative meaning |
| --- | --- |
| `requested_session` | The open Reference Market session whose completed state is requested. It is request intent and never provider proof. |
| `evidence_effective_as_of` | Provider-origin/evidence-derived date or timestamp at which the material value, classification, or lifecycle fact applies, plus an evidence-class semantics identifier naming the permitted derivation. It is never a caller-supplied eligible date. |
| `ann_date` | The existing provider announcement-date value with unchanged inclusive semantics. It is provider-derived, remains distinct from effective date and retrieval, and is never synthesized. |
| `f_ann_date` | The existing first/actual-publication value with unchanged semantics. It remains required for strict PIT where it is required today and is never inferred from `ann_date`. |
| `observed_at` | Immutable timestamp assigned to observation of the provider revision. It is never rewritten. |
| `retrieved_at` | Immutable timestamp at which the provider artifact was retrieved. It is never rewritten. |
| `eligibility_evaluated_at` | One timezone-aware run-owned instant injected before first metadata acquisition and shared by every leader/follower, retained/fresh evaluation, context bind, and checkpoint decision in the run. One context-level evaluation transaction consumes all required artifact proofs once and emits one terminal decision binding. |
| `strict_cutoff` | Exact historical availability cutoff supplied by the invoking strict-PIT contract; for session-close replay it is the pinned requested-session close unless another versioned historical contract explicitly supplies a different cutoff. |
| `exact_revision_authoritative_available_at` | Optional authoritative provider/exchange timestamp proving that the exact immutable material revision—not merely a filing generally—was available. It is qualified only when the proof identity binds the exact artifact/revision. |
| `latest_completed_session` | The session returned by the pinned Market Session Calendar revision at `eligibility_evaluated_at`. It is derived calendar authority, not provider or request evidence. |
| `retrieval_local_date` | Diagnostic rendering of `retrieved_at` in `Asia/Shanghai`. It has no eligibility authority. |

Every provider-derived metadata proof admitted to the evaluator must satisfy
all of the following before mode-specific rules are applied:

1. `observed_at <= retrieved_at`; both values are immutable and timezone-aware.
2. `evidence_effective_as_of` is non-null, is produced by the permitted
   evidence-class derivation below, and is no later than `requested_session`.
3. `ann_date` is non-null, is read from provider-origin evidence with its
   existing meaning, and is no later than `requested_session`.
4. No request field, factory default, provider route, model output, symbol,
   company/display name, retrieval date, or derived eligible session supplies
   evidence proof.

The evaluator derives the eligible session and decision. A provider, caller,
request factory, context factory, or checkpoint reader cannot supply a passing
result. Missing or contradictory proof produces a typed fail-closed rejection.
For the currently qualified classes below, `evidence_effective_as_of`,
`ann_date`, and `f_ann_date` are canonical provider dates and compare directly
and inclusively to `requested_session`. A future timestamp-valued effective
class must version and define its provider-semantic projection to a Reference
Market session/date; it cannot reuse retrieval timezone conversion as that
projection.

### Permitted `evidence_effective_as_of` and publication derivations

The following table is exhaustive for the currently relevant company-type and
listing metadata evidence classes. Any source not listed is unavailable under
`provider-neutral-temporal-eligibility-v1` until a forward evidence-class
contract explicitly qualifies its provider-origin semantics.

| Evidence class | Exact permitted effective-as-of derivation | Exact permitted publication derivation | Consequence when absent |
| --- | --- | --- | --- |
| Tushare statement-derived company type | The selected immutable statement candidate's provider `end_date` / bound `period_end` for the same row and artifact that supplies `comp_type`; the evidence-class semantics identifier records `tushare_statement_period_end`. | `ann_date` and, for strict PIT, `f_ann_date` are copied only from those exact provider filing fields on the same candidate/revision. | Missing `end_date`, `ann_date`, or artifact/revision binding rejects. Missing `f_ann_date` limits evidence to current-only at most and rejects strict PIT. |
| AKShare-Sina statement-derived company type/classifier input | The selected immutable statement row's provider report-period/end-date field already normalized into its bound period identity; the semantics identifier records `akshare_sina_statement_period_end`. | Only an explicit provider announcement/publication field on that same row/revision may populate `ann_date`; only an explicit first-publication field may populate `f_ann_date`. | If the qualified endpoint/normalizer cannot project either required field, it returns typed temporal proof unavailable. Observation or retrieval time cannot fill it. |
| Yahoo current-profile classifier input | No permitted historical effective-date or announcement-date derivation exists in the current profile contract. `retrieved_at`, the profile's current nature, and request date are not applicability proof. | None under the current evidence class. | The profile may remain a retained current diagnostic/classifier artifact, but it cannot satisfy temporally eligible company-type proof for context v2. |
| BaoStock issuer-lifecycle/listing metadata | `ipoDate`, normalized as the authoritative `listing_date`, may populate `evidence_effective_as_of` for the exact lifecycle row/artifact; the semantics identifier records `baostock_lifecycle_listing_date`. `outDate` remains a distinct lifecycle bound and never substitutes for listing applicability. | Only a provider-origin announcement/publication field explicitly bound to the same lifecycle event/revision may populate `ann_date`; `listing_date`, `observed_at`, and `retrieved_at` are not announcement aliases. Exact-revision availability proof must bind the same row/artifact. | The current `query_stock_basic` projection is insufficient if it lacks provider announcement proof; the evaluator fails closed until 02H projects qualified provider-origin proof. No request-date or listing-date backfill is allowed. |

This provider-neutral contract deliberately permits an evidence class to be
unavailable. It does not manufacture proof to keep a route successful. The
source projection retains its original provider field identities and artifact/
row/revision bindings so audit and replay can verify the derivation exactly.

### One temporal evaluator and mode-specific predicates

Add one closed evaluator contract,
`provider-neutral-temporal-eligibility-v1`, and one immutable decision
projection, `financial-metadata-temporal-eligibility-decision-v1`. All fresh and
retained company-type evidence, fresh and retained listing/lifecycle metadata,
Financial Eligibility Acquisition Context construction, statement/indicator
request binding, checkpoint validation, and exact replay must use this evaluator
or validate its persisted decision identity. No retained/post-route equality
validator remains an authority.

For `current_analysis_only`, common provider proof must pass and all of the
following are required:

1. `requested_session` is an open session in the pinned calendar revision.
2. `retrieved_at >= session_close(requested_session)`.
3. `retrieved_at <= eligibility_evaluated_at`.
4. `latest_completed_session(pinned Market Session Calendar,
   eligibility_evaluated_at) == requested_session`.

The lower boundary is inclusive at the requested session close. The prior
frontier's upper boundary is exclusive at the next open session's close: the
calendar regards that next session as completed at its close, at which instant
`latest_completed_session` advances. Local midnight, the next-session open,
and a fixed elapsed duration have no authority. Therefore a retrieval at
`2026-08-11 00:48:07 Asia/Shanghai` may remain eligible for requested session
`2026-08-10`; retrieval immediately before or during the next open session may
also remain eligible; at the next session's close, a new current run for
`2026-08-10` is rejected.

Weekends and holidays follow only the pinned authoritative calendar. Friday
remains the frontier through Saturday and Sunday and during Monday until Monday
completes. A session preceding a mainland holiday remains the frontier
throughout the non-session interval and during the next open session until that
session completes. No wall-clock grace duration is computed.

For `strict_historical_pit`, common provider proof must pass and all of the
following are required:

1. `f_ann_date` exists and `f_ann_date <= requested_session`.
2. Either both `observed_at <= strict_cutoff` and
   `retrieved_at <= strict_cutoff`, or a qualified
   `exact_revision_authoritative_available_at <= strict_cutoff` binds the exact
   immutable revision.

A generic filing/announcement date does not prove availability of a later
corrected revision. Post-cutoff observation or retrieval does not become strict
PIT because the current-analysis frontier still equals `requested_session`.
Missing first-publication or exact-revision availability proof remains
fail-closed. Financial-period eligibility v2 and the current-only versus strict-
PIT distinction remain otherwise unchanged.

The exact rule
`retrieved_at.astimezone(Asia/Shanghai).date() == requested_session` is retired
as an eligibility authority in the company-type acquisition evidence validator,
retained company-type compatibility filter, post-route company-type validator,
and equivalent retained listing-metadata check. It may survive only as a safe
diagnostic projection. It must not be replaced with a local-date `<=` or `>=`
comparison or another scattered temporal predicate.

The decision result is exactly `accepted` or `rejected`; an accepted decision
also derives `eligible_session=requested_session`, while a rejected decision
has no eligible session. Rejection reasons are a sorted, duplicate-free tuple
from this closed v1 vocabulary:

| Reason | Exact condition |
| --- | --- |
| `requested_session_not_open_in_pinned_calendar` | The requested session is absent or not open in the pinned calendar revision. |
| `calendar_eligibility_binding_mismatch` | The supplied calendar identity/session/close/frontier inputs do not match their content-derived binding. |
| `observation_after_retrieval` | `observed_at > retrieved_at`. |
| `evidence_effective_as_of_unproven` | Effective proof is null or its evidence-class derivation is not permitted/qualified. |
| `evidence_effective_after_requested_session` | Provider-derived effective proof is later than the requested session. |
| `ann_date_missing` | Provider `ann_date` is null. |
| `ann_date_after_requested_session` | Provider `ann_date` is later than the requested session. |
| `retrieval_before_requested_session_completed` | Current-analysis retrieval is earlier than the pinned requested-session close. |
| `retrieval_after_eligibility_evaluation` | Current-analysis retrieval is later than the fixed evaluation instant. |
| `requested_session_not_latest_completed_frontier` | The pinned calendar's derived latest-completed session at evaluation differs from the request. |
| `f_ann_date_missing_for_strict_pit` | Strict mode lacks first/actual-publication proof. |
| `f_ann_date_after_requested_session` | Strict-mode `f_ann_date` is later than the requested session. |
| `exact_revision_not_available_by_strict_cutoff` | Neither direct observation/retrieval nor qualified exact-revision availability is no later than `strict_cutoff`. |
| `temporal_evidence_artifact_binding_mismatch` | Effective/publication/availability proof does not bind the evaluated immutable artifact/row/revision. |
| `unknown_temporal_eligibility_mode` | The closed mode is neither current-analysis-only nor strict historical PIT. |

No reason in this vocabulary authorizes fallback or provider ordering. Adding,
removing, or changing a reason condition requires a new temporal policy version;
unknown reasons fail checkpoint compatibility.

### Provider-neutral company-type resolution

- Add `financial_company_type_evidence` as a prerequisite capability in the
  next version of the one Capability Routing Plan contract. Its qualified
  shadow route is `tushare -> akshare_sina -> yfinance`; the plan is immutable
  after creation, and no composition/factory table may replace or reorder that
  route.
- The capability accepts only authoritative Instrument Identity, requested
  session or
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
  value and no exact compatible retained projection exists. Retained and fresh
  projections pass through the same temporal evaluator using the run's fixed
  `eligibility_evaluated_at`; retained acceptance from an older run is not
  rebound after the latest-completed frontier advances. Every physical call
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
- Both retained and fresh lifecycle projections must carry the provider-neutral
  temporal proof and decision defined above. The existing comparison between a
  retained projection's retrieval-local date and the request date is retired;
  `ipoDate`/listing date is not allowed to synthesize `ann_date`, and a projection
  without qualified publication proof fails typed rather than backfilling from
  the request.

### Fresh-run acquisition and pure request factories

Blueprint validation and managed-runtime assembly remain provider-I/O-free. On
the first current qualified financial request in a fresh
`qualified_v1_shadow` run, the graph/run host starts one context-acquisition
single-flight. Before the leader evaluates retained state or performs provider
work, the run host injects one fixed `eligibility_evaluated_at` and pins the
Market Session Calendar eligibility binding. Concurrent callers join the same
operation and receive the same terminal context-level temporal decision binding
and artifact subdecisions; no follower invokes the evaluator, samples a clock,
or can split the context at a midnight/session-close boundary. The
acquirer first uses exact
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
routing request`. They validate binding equality and project context values as
request intent only;
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

### Financial-indicator semantic metadata contract

The QCF4 metadata-compatibility prototype is authoritative design evidence for
this amendment. Adopt Design A's provider-neutral semantic model together with
Design C's fallback behavior. The compatibility model is applied to each
retained normalized field under a versioned declaration; it is never inferred
from provider identity or from the union/subset of fields populated in a
particular response.

Keep the following dimensions separate throughout normalization, compatibility,
selection, manifest, checkpoint, report, and audit projection:

| Dimension | Normative meaning |
| --- | --- |
| `instrument_currency` | Currency in authoritative Instrument Identity. It binds issuer/instrument context and remains `CNY` for the mainland Equities in scope, including when a normalized value is dimensionless. It is not provider value-currency evidence. |
| `value_unit` | The provider-origin unit/basis and canonical normalized unit. Percentage/rate input may normalize deterministically from `PERCENT` to `RATIO`; currency/share, monetary scale, shares, days, and turns/reporting-period remain explicit when applicable. |
| `value_currency` applicability | Whether a currency is semantically part of the normalized value. It applies to monetary and per-share values and does not apply to dimensionless ratios, percentages/rates, share counts, or days/turnover-time values. |
| `filing_consolidation_scope` | The scope of financial inputs represented by an indicator. It is compared with requested scope only when the registered retained field declares scope material. |

Use the following closed semantic assessment states for each materiality-aware
dimension:

| State | Normative meaning and compatibility behavior |
| --- | --- |
| `PROVEN` | Qualified provider-origin evidence proves the material dimension and its value matches the requested capability contract. |
| `UNPROVEN` | The dimension is material, but the retained provider artifact does not prove it. It is incompatible and cannot satisfy selection/admission. |
| `NOT_APPLICABLE` | The versioned field-semantics registry declares that the dimension cannot change the normalized value's semantic meaning. Only this explicit policy declaration allows the dimension to be ignored. |

`UNKNOWN` remains a raw provider marker or enum value indicating that the
provider did not identify a value. It is retained independently from the
assessment state. It is never proof, is never automatically compatible, and is
never converted to `PROVEN`, `NOT_APPLICABLE`, `CNY`, or `CONSOLIDATED`.
`UNPROVEN` is an assessment about missing material proof; `NOT_APPLICABLE` is a
semantic policy result; neither is an alias for raw `UNKNOWN`.

The Financial Eligibility Acquisition Context continues carrying requested
currency and consolidation scope solely as immutable run intent. Its CNY value
also agrees with authoritative Instrument Identity. Request factories may copy
these values only into typed provider requests as request intent. They must
never relabel them as provider metadata, populate provider artifacts/candidates
from them, or use them to repair absent provider evidence.

### Versioned indicator field classification policy

Add closed policy data named `financial-indicator-field-semantics-v1`, keyed by
normalized field plus the financial ratio declaration version. It records the
field class, canonical unit/basis, value-currency applicability,
filing-consolidation-scope materiality, provider evidence requirements,
current-only eligibility, and strict-PIT eligibility. No runtime branch may
derive these properties from populated response fields.

| Class | Value currency | Expected normalized unit/basis | Filing scope | Required provider evidence | Current-only eligibility | Strict-PIT eligibility |
| --- | --- | --- | --- | --- | --- | --- |
| Dimensionless ratio | `NOT_APPLICABLE` | `RATIO` | Material for every currently declared field in this class | Numeric value, provider field, original unit/basis, deterministic normalized `RATIO`, announcement metadata, and proven scope | Eligible only when material metadata and unchanged completeness/current gates pass; absent first-publication proof limits the result to current-only at most | Same hard gates plus qualified first-publication and observed-revision proof at/before cutoff |
| Percentage/rate | `NOT_APPLICABLE` | `RATIO`, with provider `PERCENT`/rate basis retained | Material | Numeric value, percent/rate basis, deterministic normalization to `RATIO`, announcement metadata, and proven scope | Same as dimensionless ratio | Same plus strict-PIT lineage |
| Per-share | Required | `CURRENCY_PER_SHARE` with share basis | Material | Numeric value, provider-proven value currency/share unit, share basis, announcement metadata, and proven scope | Eligible only when currency, unit/basis, scope, completeness, and current gates pass | Same plus strict-PIT lineage |
| Monetary | Required | `CURRENCY_BASE_UNIT` plus explicit provider monetary scale | Material | Numeric value, provider-proven value currency, monetary unit/scale, announcement metadata, and proven scope | Eligible only when currency, unit/scale, scope, completeness, and current gates pass | Same plus strict-PIT lineage |
| Share-count | `NOT_APPLICABLE` | `SHARES` plus issued/diluted/weighted-average basis | `NOT_APPLICABLE` only for issuer share-count facts | Numeric value, shares unit, share-count basis, effective date, and publication/observation lineage | Eligible when basis, lineage, completeness, and current gates pass | Same plus strict-PIT lineage |
| Days/turnover-time | `NOT_APPLICABLE` | `DAYS` or `TURNS_PER_REPORTING_PERIOD`, as declared | Material for every currently declared field in this class | Numeric value, provider time/turnover basis, announcement metadata, and proven scope | Eligible only when unit/basis, scope, completeness, and current gates pass | Same plus strict-PIT lineage |
| Other | Field-specific; fail closed | Explicit registry value | Field-specific; fail closed | An explicit registry entry and all evidence named by it | Ineligible until classified | Ineligible until classified and strict-PIT lineage passes |

The complete v1 registry for every currently declared normalized family-field
pair is:

| Family | Class | Normalized fields | Filing scope materiality |
| --- | --- | --- | --- |
| Profit | Per-share | `earnings_per_share` | Material; must be `PROVEN` |
| Profit | Percentage/rate | `ebitda_margin`, `gross_margin`, `net_margin`, `operating_margin`, `pre_tax_margin`, `profit_to_cost`, `return_on_assets`, `return_on_equity`, `return_on_invested_capital` | Material; must be `PROVEN` |
| Operation | Days/turnover-time | `accounts_payable_turnover`, `asset_turnover`, `cash_conversion_cycle`, `current_asset_turnover`, `fixed_asset_turnover`, `inventory_days`, `inventory_turnover`, `operating_cycle`, `receivable_days`, `receivables_turnover` | Material; must be `PROVEN` |
| Growth | Percentage/rate | `asset_growth`, `basic_earnings_per_share_growth`, `cash_flow_growth`, `equity_growth`, `gross_profit_growth`, `liability_growth`, `net_income_growth`, `operating_profit_growth`, `parent_net_income_growth`, `revenue_growth`, `return_on_equity_growth`, `total_profit_growth`, `working_capital_growth` | Material; must be `PROVEN` |
| Balance | Dimensionless ratio | `cash_ratio`, `current_ratio`, `debt_to_assets`, `debt_to_equity`, `equity_multiplier`, `interest_coverage`, `long_term_debt_ratio`, `quick_ratio`, `tangible_asset_ratio`, `working_capital_ratio` | Material; must be `PROVEN` |
| Cash flow | Dimensionless ratio | `cash_flow_adequacy`, `cash_flow_to_debt`, `cash_flow_to_gross_revenue`, `cash_flow_to_liabilities`, `cash_flow_to_revenue`, `current_assets_to_assets`, `interest_coverage`, `non_current_assets_to_assets`, `operating_cash_flow_ratio`, `quality_of_income`, `reinvestment_ratio`, `self_financing_ratio`, `tangible_asset_ratio` | Material; must be `PROVEN` |
| Cash flow | Percentage/rate | `cash_return_on_assets`, `free_cash_flow_margin` | Material; must be `PROVEN` |
| DuPont | Days/turnover-time | `asset_turnover_component` | Material; must be `PROVEN` |
| DuPont | Dimensionless ratio | `debt_burden`, `equity_multiplier_component`, `financial_leverage`, `interest_burden`, `parent_net_income_share`, `tax_burden` | Material; must be `PROVEN` |
| DuPont | Percentage/rate | `ebit_margin_component`, `net_profit_margin_component`, `operating_margin_component`, `return_on_assets_component`, `return_on_equity_component` | Material; must be `PROVEN` |

This is 70 family-field pairs / 68 unique normalized fields across legacy v1,
qualified Tushare v2, and BaoStock v1 declarations. No currently normalized
field is monetary, share-count, or `other`. BaoStock raw-only `netProfit`,
`MBRevenue`, `totalShare`, `liqaShare`, `NRTurnDays`, and `INVTurnDays` remain
provider artifact data and are not promoted by this amendment.

### Currency, consolidation scope, and companion-filing rules

- For dimensionless ratios and percentage/rate fields, authoritative Instrument
  Identity remains CNY while value currency is `NOT_APPLICABLE`. A provider
  `XXX` marker is retained but cannot conflict with CNY monetary evidence because
  no monetary value-currency dimension exists for that normalized value.
- For per-share and monetary fields, value currency remains material. The
  provider must prove currency and applicable unit/scale; missing, `UNKNOWN`,
  `XXX`, request-derived, or otherwise unproven currency remains fail-closed or
  degraded according to the existing evidence contract.
- Filing consolidation scope is material for every currently normalized
  statement-derived indicator field in the registry because parent and
  consolidated inputs can differ. Compatibility requires provider-origin proof
  of the requested scope. An absent or raw `UNKNOWN` scope assesses as
  `UNPROVEN`, never as `CONSOLIDATED`.
- `NOT_APPLICABLE` filing scope is reserved for fields whose versioned semantics
  make scope irrelevant, such as a future issuer share-count fact. It cannot be
  assigned merely because a value is a ratio, complete, or currency-free.
- Design B is rejected for this version. Statement metadata may not be bound
  onto an indicator artifact unless a future qualified provider contract
  supplies an exact stable filing/report identity shared across both artifacts,
  including filing revision. Matching provider, Instrument Identity, company
  type, reporting period, or observation time is insufficient. Tushare
  `update_flag` is not restatement lineage. No inferred cross-artifact lineage
  is permitted.

For the diagnosed Tushare `fina_indicator` artifacts, normalized value
completeness can be 100%. Value currency is `NOT_APPLICABLE` for their retained
dimensionless ratio, percentage/rate, and turnover fields. Filing consolidation
scope remains `UNPROVEN` because the endpoint does not prove it. Every requested
family in the v1 registry requires proven scope, so those candidates are
retained and remain unadmitted/unselected. Missing `f_ann_date` independently
limits the artifacts to current-only at most and forbids a strict no-lookahead
claim, strict historical Source Fact, and strict replay eligibility.

### Compatibility assessment, selection, and fallback

Add `financial-indicator-metadata-compatibility-v1` as a closed provider-neutral
assessment bound to the candidate, provider artifact, period, family, retained
field set, Instrument Identity, requested capability metadata, declaration
version, and `financial-indicator-field-semantics-v1`. For each applicable
dimension it retains the raw provider marker/value, assessment state, safe
evidence origin, expected semantic value when material, and a canonical typed
reason. The assessment identity commits to all of those bindings.

Compatibility is a separate hard gate from the unchanged 100% critical and
at-least-90% declared-core completeness gates. A complete family with any
material `UNPROVEN` or mismatched dimension is not selected. Its artifact,
candidate, completeness assessment, metadata assessment, and typed rejection
remain in the manifest. `NOT_APPLICABLE` is ignored only when the pinned field-
semantics contract explicitly declares the dimension irrelevant for every
retained selected field.

Preserve Design C fallback behavior without adding router logic. The existing
Capability Routing Plan remains the sole route and work authority. After
Tushare evaluation, only unresolved indicator families continue sequentially to
AKShare, BaoStock, and Yahoo. Each lower provider can satisfy a family only
through its own truthful artifact-bound metadata contract. The router contains
no Tushare/AKShare/BaoStock/Yahoo compatibility exception, expected-currency
table, request-metadata substitution, provider fan-out, or changed stop rule.

Evidence Admission receives only selected evidence whose applicability has
already been normalized under the pinned compatibility contract. No broad
Evidence Admission rewrite or evidence-contract version is required. Its strict
trust boundary remains unchanged: material `UNPROVEN` metadata is rejected,
`NOT_APPLICABLE` is ignored only under the explicit pinned capability contract,
and `UNKNOWN` is never compatible proof. Only selected candidates can project a
Source Fact or Decision-Ready evidence. Metadata applicability never bypasses
publication, observation, availability-scope, or strict-PIT requirements.

### Exact contract versions and migration behavior

The following labels are the exact QCF4 semantic-compatibility versions and
remain required material bindings. The temporal amendment then advances every
affected top-level/context-bound writer through the second table below.
“Unchanged” means no new version is created; it does not permit reinterpretation
under either policy.

| Contract | Required new-write version and binding |
| --- | --- |
| Financial Period Candidate / Financial Period Identity | Keep raw observation contracts and identity namespaces at contract `1.0`, `financial-period-candidate:v1`, and `financial-period:v1`. They continue retaining provider-origin `currency`/scope markers exactly as observed. Do not add applicability state to, or reinterpret, these immutable raw identities. |
| Tushare indicator normalization/artifact | Keep `tushare-financial-indicator-adapter-v1` normalized artifacts and existing candidate identities. Split/version only the adapter result/assessment projection as `tushare-financial-indicator-result-v2` so it can carry `financial-indicator-metadata-compatibility-v1`; do not relabel stored v1 artifacts. |
| AKShare indicator normalization | Write `akshare-financial-indicators-v2`; absent provider value currency becomes raw `XXX`, absent scope remains raw `UNKNOWN`, and request/Instrument Identity values are not provider proof. |
| BaoStock ratio normalization | Write `baostock-ratio-family-normalizer-v2`; apply the same no-request-backfill rule while retaining BaoStock artifact evidence truthfully. Keep the adapter transport/result envelope `baostock-ratio-family-adapter-v1`; compatibility is assessed at the shared selection seam, so stored v1 artifacts/results are not relabeled. |
| Indicator field-semantics policy | Add `financial-indicator-field-semantics-v1` with the complete registry above. |
| Metadata compatibility | Add `financial-indicator-metadata-compatibility-v1` and identity namespace `financial-indicator-metadata-compatibility:v1`. |
| Indicator period compatibility/completeness selection | Add `financial-period-selection-v3`. It preserves completeness math and financial-period eligibility v2 while binding the metadata-assessment identity and typed incompatibility reasons. Statement selection contracts remain unchanged. |
| Indicator Financial Provider Candidate and period selection/rejection | Add `financial-provider-candidate-v2` and `financial-period-selection:v3` bindings. Indicator candidate wrappers, accepted/current-only selections, and typed rejections bind the compatibility assessment identity; raw candidate identities remain v1. Statement wrappers/selections remain unchanged. |
| Indicator financial manifest | Add `financial-manifest-v3` and `financial-manifest:v3`; it retains field-semantics/compatibility versions, assessment identities/states, raw-marker-safe diagnostics, and typed incompatibility reasons. Statement manifests remain `financial-manifest-v2`. |
| Indicator routing result | Add `financial-indicator-routing-result-v3`; selected and unresolved families must agree exactly with manifest-v3 compatibility outcomes. Statement routing behavior remains on its existing contracts. |
| Indicator dispatch/checkpoint entry | Add `financial-indicator-selection-dispatch-v2` and `financial-indicator-selection-checkpoint-v2`; both bind routing-result v3 and manifest-v3 identities. |
| Dispatch checkpoint ledger | Ledger `1.5` is the exact semantic-compatibility predecessor, carrying statement manifest v2, indicator manifest v3, field-semantics and metadata-compatibility versions, all assessment identities/states, and the context/cache/plan bindings. Temporal new writes advance it to `1.6` below. |
| Indicator report/audit operational projections | Add `financial-completeness-row-v4`, `financial-aggregate-completeness-v3`, `financial-manifest-audit-v3`, `financial-manifest-request-audit-v3`, and Financial Dispatch Audit Projection `3.0`. They bind indicator manifest v3, selection v3, the pinned semantics/compatibility versions, assessment identities/states, and safe typed reasons without normalized provider values or raw payloads. Statement projections remain on their existing versions. |
| Indicator tool manifest projection/envelope | Add `financial-tool-manifest-v3` and `financial-tool-message-envelope-v3` with the same safe bindings. Statement tool projections remain v2. Canonical Evidence, Source Fact, Decision Report, and Decision Audit contracts do not change. |
| Capability Routing Plan | Plan payload `1.2` is the exact semantic-compatibility predecessor with indicator-specific bindings for `financial-indicator-field-semantics-v1`, `financial-indicator-metadata-compatibility-v1`, `financial-manifest-v3`, and `financial-period-selection-v3`. Existing statement manifest/selection bindings remain v2. Temporal new writes advance the payload to `1.3` below; provider routes, financial-period eligibility v2, completeness thresholds, and the `mainland-routing-plan:v1:<sha256>` signature namespace remain unchanged. |

The exact temporal forward-only version set is:

| Contract | Required temporal new-write version and material binding |
| --- | --- |
| Capability Routing Plan | `1.3`; pins `provider-neutral-temporal-eligibility-v1`, `market-session-eligibility-binding-v1`, and every downstream temporal version below in addition to all plan-1.2 semantic bindings. The `mainland-routing-plan:v1:<sha256>` namespace stays stable and its digest changes because these bindings are material. |
| Temporal policy/evaluator | Add `provider-neutral-temporal-eligibility-v1`; its closed mode, reason, boundary-inclusivity, and provider-proof rules are immutable for the version. |
| Calendar eligibility binding | Add `market-session-eligibility-binding-v1` and identity namespace `market-session-eligibility-binding:v1:`; bind calendar revision ID, Reference Market/timezone, requested session/open status, requested close, next open session/close, fixed evaluation instant, and derived latest-completed session. |
| Temporal decision projection | Add `financial-metadata-temporal-eligibility-decision-v1` and identity namespace `financial-metadata-temporal-eligibility-decision:v1:`; bind mode, artifact/revision, all proof fields, policy/calendar identities, result, and typed reasons. |
| Company-type acquisition evidence | Advance `financial-company-type-evidence-binding-v2` to `financial-company-type-evidence-binding-v3`; add provider-origin effective/publication derivation references and immutable timestamps without relabeling v2 evidence. |
| Eligibility metadata binding | Advance `financial-eligibility-metadata-binding-v1` to `financial-eligibility-metadata-binding-v2`; replace request-only as-of ambiguity with `requested_session`, mode, fixed evaluation time, calendar binding, and strict cutoff where applicable. |
| Company-type evidence projection | Advance `financial-company-type-evidence-projection-v1` to `financial-company-type-evidence-projection-v2`; embed the temporal-decision identity and exact evidence derivation binding. |
| Listing metadata and retained projection | Advance `financial-listing-metadata-v1` and `financial-listing-metadata-projection-v1` to `financial-listing-metadata-v2` and `financial-listing-metadata-projection-v2`; carry provider-origin effective/publication proof and the same temporal decision rather than local-date equality. |
| Company-type routing request/response | Advance `financial-company-type-evidence-routing-request-v1` to `financial-company-type-evidence-routing-request-v2` and `financial-company-type-evidence-provider-response-v1` to `financial-company-type-evidence-provider-response-v2`; request v2 carries metadata binding v2, and an available response v2 carries evidence v3 plus decision v1. |
| Financial Eligibility Acquisition Context | Advance `financial-eligibility-acquisition-context-v1` to `financial-eligibility-acquisition-context-v2` and identity namespace to `financial-eligibility-acquisition-context:v2:`; bind both prerequisite temporal decisions and the latest-completed-session decision. |
| Eligibility request binding and result/failure | Advance `financial-eligibility-request-binding-v1` to `financial-eligibility-request-binding-v2` and `financial-eligibility-acquisition-result-v1` to `financial-eligibility-acquisition-result-v2`; v2 failures include closed temporal reason codes and may retain a safe rejected decision without creating a partial context. |
| Statement and indicator routing requests | Advance `financial-statement-routing-request-v3` to `financial-statement-routing-request-v4` and `financial-indicator-routing-request-v6` to `financial-indicator-routing-request-v7`; both bind context v2 and temporal decision identities without letting factories recompute eligibility. |
| Routing results and manifests | Advance `financial-statement-routing-result-v1` to `financial-statement-routing-result-v2`, `financial-indicator-routing-result-v3` to `financial-indicator-routing-result-v4`, and both statement/indicator new-write manifests to `financial-manifest-v4` / `financial-manifest:v4`; v4 retains the applicable temporal decision identities/results/reasons while preserving the indicator semantic-compatibility bindings. |
| Selection dispatch/checkpoint | Advance `financial-statement-selection-dispatch-v1` to `financial-statement-selection-dispatch-v2`, `financial-statement-selection-checkpoint-v1` to `financial-statement-selection-checkpoint-v2`, `financial-indicator-selection-dispatch-v2` to `financial-indicator-selection-dispatch-v3`, and `financial-indicator-selection-checkpoint-v2` to `financial-indicator-selection-checkpoint-v3`. |
| Provider Subrequest Cache checkpoint | Advance `1.1` to `1.2`; bind plan 1.3, metadata binding/context v2, calendar binding, artifact identity, and accepted/rejected temporal decision identity. |
| Financial dispatch ledger | Advance `1.5` to `1.6`; persist context v2, manifest v4, temporal policy/calendar versions, fixed evaluation inputs, decisions/results/reasons, and existing semantic-compatibility/attempt state. |
| Manifest/audit/report operational projections | Advance `financial-completeness-row-v4` to `financial-completeness-row-v5`, `financial-aggregate-completeness-v3` to `financial-aggregate-completeness-v4`, `financial-manifest-audit-v3` to `financial-manifest-audit-v4`, `financial-manifest-request-audit-v3` to `financial-manifest-request-audit-v4`, Financial Dispatch Audit Projection `3.0` to `4.0`, `financial-tool-manifest-v3` to `financial-tool-manifest-v4`, and `financial-tool-message-envelope-v3` to `financial-tool-message-envelope-v4` wherever temporal decisions surface. These carry only safe decision identity/result/reasons and never raw payloads or credentials. `mainland-financial-report-projection-v2`, Canonical Evidence, Source Fact, Decision Report, and Decision Audit contracts remain unchanged and do not recompute or become authorities for temporal eligibility; qualified shadow report diagnostics consume the safe v4 operational projections. |

Historical artifacts, candidates, manifests, routing results, tool projections,
audits, reports, and checkpoints remain readable under their recorded versions
and are never rewritten. Historical replay uses the semantic/compatibility and
temporal contracts pinned in the recorded plan/checkpoint; a reader must not
apply the new registry or temporal frontier retroactively. New qualified runs
write plan 1.3 and the temporal new-write contracts above while retaining every
unchanged plan-1.2 semantic binding.

An attempted plan-1.3 restore from a checkpoint that carries plan 1.2/context
v1, an old/absent metadata-compatibility contract, or an old/absent temporal
policy must fail with a typed compatibility error or require the explicit
existing `START_NEW` choice. `START_NEW` allocates a new run identity and
performs the fresh path. Restore performs zero provider/model I/O, does not
consult the current registry or today's calendar frontier to repair old state,
and never upgrades a checkpoint in place.

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
selection, completeness, and capability-response seams with the exact
forward-version changes defined in this amendment. No raw frame or SDK object
crosses the adapter boundary. Factor and name calls share the run cache but
retain distinct endpoint scopes, subrequest keys, artifacts, outcomes, and
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
  qualified checkpoint projection advances to ledger `1.6` and carries the safe
  Financial Eligibility Acquisition Context v2 projection, temporal policy and
  Market Session Calendar binding, accepted/rejected temporal decisions, plus
  pinned indicator field-semantics and metadata-compatibility versions/
  assessment identities; no second checkpoint file, sidecar, or competing
  checkpoint owner is introduced.
- The checkpoint binds the context contract version, deterministic context
  identity, complete safe context fields, run/asset signatures, authoritative
  Instrument Identity, `requested_session`, temporal mode,
  `eligibility_evaluated_at`, `evidence_effective_as_of`, `ann_date`, applicable
  `f_ann_date`, immutable `observed_at`/`retrieved_at`, requested availability
  scope, financial
  eligibility-policy binding, routing-plan signature, calendar revision and
  target/next-session completion binding, derived latest-completed session,
  company-type resolution/evidence/temporal-decision identity, listing date/
  provenance/temporal-decision identity, and canonical acquisition metadata.
  Strict mode additionally binds `strict_cutoff` and qualified
  exact-revision availability proof when used. Context identity therefore
  cannot be detached from the cache, dispatcher selection state, or historical
  request identities it governed.
- On exact compatible resume, validate every context binding together with run
  scope, provider-chain topology, immutable artifacts, attempt identities,
  Provider Subrequest Cache, field-semantics policy, metadata-compatibility
  assessment identities/states, manifests, and dispatcher state before any
  adapter, request factory, or dispatcher becomes callable. Restore and single-
  assign the immutable context, restore terminal cache entries from existing
  immutable artifact references, then restore dispatcher results. The exact
  repeated request returns identical context, cache, attempts, candidates,
  metadata assessments, typed rejections, selections, artifacts, and IDs with
  zero provider/network I/O, zero coordinator permits, zero new attempt events,
  and zero model I/O. Replay reproduces the recorded temporal decision directly
  from its deterministic recorded inputs/result, does not sample the restore
  clock, and does not call `latest_completed_session` against today's calendar.
  Local checkpoint/artifact reads are the restoration boundary.
- A missing context projection, unknown/ambiguous/contradictory company-type
  result, non-authoritative or mismatched listing provenance, context identity or
  version mismatch, malformed context, plan/asset/requested-session/temporal-
  mode/availability/eligibility mismatch, or corrupt cache/artifact projection
  fails typed before external
  work. Restoration does not call the provider-neutral acquirer, silently
  reconstruct context from live providers, delete bad state, reuse a convenient
  subset, or rewrite the historical checkpoint.
- Interrupted in-flight entries are never serialized as available. Checkpointing
  occurs only at the existing safe terminal boundary; otherwise the existing
  unsafe-checkpoint failure is preserved.
- Legacy and historical qualified checkpoints remain readable under their
  recorded paths and pinned contracts. A plan-1.2 checkpoint carrying its
  recorded semantic-compatibility state remains readable only as its original
  plan-1.2 run. A plan-1.3 run cannot resume a plan-1.2/context-v1 checkpoint:
  it follows the explicit typed temporal incompatibility/`START_NEW` rule
  without live repair. Choosing `START_NEW`
  requires a new run identity and the fresh acquisition path; it is never an
  implicit repair of a restoration. No historical checkpoint receives an
  in-place upgrade or semantic reinterpretation.

### Historical and shadow compatibility

- Do not rewrite, relabel, migrate in place, delete, or renumber historical
  reports, checkpoints, artifacts, provider rows, physical attempts, snapshots,
  pins, manifests, audits, run IDs, request IDs, sequence IDs, or artifact IDs.
- New runs use the exact forward schemas and identity constructors listed in the
  version table while preserving every unchanged historical namespace. The
  static factory identity may be reported as safe operational metadata, but it
  is not part of evidence, route policy, or an existing content identity unless
  an explicitly versioned contract requires it.
- New temporal decisions never relabel a historical artifact as eligible or
  ineligible. The failed Ticket 07 `601328.SS` run, its rejected evidence,
  checkpoint/report/audit state, run ID, and attempts remain immutable and are
  interpreted only under their recorded exact-local-date contract.
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
- Temporal contract tests exercise the one provider-neutral evaluator at the
  highest metadata-admission seam. They use a pinned fake Market Session
  Calendar and fixed injected `eligibility_evaluated_at`, cover both fresh and
  retained company-type/listing evidence, and assert observable decisions and
  typed reasons rather than duplicating private date comparisons.
- Provenance tests enumerate every permitted `evidence_effective_as_of`
  derivation and reject request/session/factory-default/model/route/name/ticker/
  retrieval-date backfill. They prove `observed_at` and `retrieved_at` remain
  byte-for-byte unchanged, provider `ann_date` remains distinct, and missing
  exact first-publication/revision proof fails strict PIT.
- Indicator contract tests enumerate the entire pinned field registry and test
  compatibility at the highest existing candidate-selection seam. They prove
  that response sparsity cannot change classification; `UNKNOWN`, `UNPROVEN`,
  and `NOT_APPLICABLE` remain distinct; CNY Instrument Identity does not become
  value-currency proof; request currency/scope never enters provider evidence;
  and material unproven scope retains/rejects a complete candidate while the
  plan advances only its unresolved family.
- Migration tests open each historical candidate/manifest/routing/checkpoint
  version under its recorded contract, then prove that a new plan-1.3 run writes
  only the exact temporal versions while retaining plan-1.2 semantic bindings.
  A plan-1.3 restore from plan-1.2/context-v1 or old/absent temporal state must
  fail typed or require explicit `START_NEW`, with no repair, provider/model I/O,
  historical rewrite, current-registry reinterpretation, or consultation of
  today's calendar frontier.
- The real two-symbol gate is deliberately separate from deterministic
  acceptance. It runs only after all non-live criteria pass and only with
  explicit operator authorization.

### Deterministic acceptance criteria

QCF1–QCF18 retain their existing IDs and authority. QCF1–QCF3 and QCF5–QCF18
retain their scenarios and pass/fail meaning; QCF4 alone receives the explicit
metadata-compatibility correction below. The following fresh-origin condition
strengthens, but does not waive or replace, QCF4–QCF12:

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
- QCF4 is the corrected metadata-compatibility prerequisite for QCF5–QCF12.
  Those criteria remain blocked and are not considered runnable/passing until
  QCF4 proves the new contract at the fresh built-in seam. This dependency does
  not renumber, waive, or weaken any QCF.

| ID | Scenario | Required result |
| --- | --- | --- |
| QCF1 | Start CLI and programmatic `qualified_v1_shadow` validation with qualified capabilities enabled, no injected composition, and no explicit factory path. | The statically registered built-in factory is selected, the plan-bound managed composition reaches the graph, and `qualified_routing_composition_missing` is absent. No test module or dynamic-import fixture participates. |
| QCF2 | Supply a direct composition, then separately an explicit operator factory. | Direct composition wins without invoking either factory; explicit operator factory wins over the built-in; the existing safe-config and plan-signature validations remain exact. |
| QCF3 | Run legacy mode, ordinary non-validation shadow mode, an empty/non-applicable qualified plan, and attempted `qualified_v1`. | Legacy/non-validation paths construct no qualified resource and perform zero qualified I/O; `qualified_v1` fails with the existing authority-disabled preflight before the built-in factory is called. |
| QCF4 | From the required fresh origin, return complete fixed Tushare company-type, statement, and indicator fixtures plus authoritative BaoStock lifecycle metadata for all eligible targets; configure deterministic lower-provider indicator fixtures for unresolved families. | The run originates and binds fresh company-type/listing context before ordinary dispatch. Complete Tushare statement artifacts whose own metadata qualifies are retained and selected and stop lower statement fallback. Complete Tushare indicator artifacts/candidates and 100% value-completeness assessments are retained, but their ratio/rate/turnover value currency is `NOT_APPLICABLE`, filing consolidation scope is material and `UNPROVEN`, and missing `f_ann_date` remains current-only at most; no affected Tushare indicator family is selected. Only those unresolved indicator families continue deterministically through the immutable Capability Routing Plan, and a lower provider may satisfy them only through its own truthful metadata contract. Requested `CNY`/`CONSOLIDATED` is absent from provider evidence. Exact prerequisite and every ordinary physical-attempt cardinality—including lower-provider attempts for actual unresolved work—are proven, with no hidden calls, duplicate bulk work, provider-specific router branch, or fan-out. |
| QCF5 | From the required fresh origin, make one Tushare endpoint unavailable and another return usable rows plus partial eligible-period/family gaps. | One context is originated without a seed; successful Tushare artifacts and periods survive; only unresolved statement periods or indicator families continue; typed gaps and all prerequisite/ordinary attempts are exact; no successful bulk request is repeated per target. |
| QCF6 | From the required fresh origin, leave statement gaps after Tushare and make AKShare-Sina return usable periods, including partial salvage across its statement types. | Context acquisition resolves company type once before factory use; AKShare-Sina is called sequentially only for unresolved ordinary work, its usable artifacts/periods are retained, and Yahoo is not called for resolved targets. |
| QCF7 | From the required fresh origin, leave eligible statement or indicator gaps after higher providers and provide only Yahoo fallback data. | The context is self-originated first; Yahoo is last, coordinated, and selected only under existing degraded/current-only contracts; candidate-free current profile and Yahoo-derived factor behavior remain exact; no Yahoo evidence becomes strict PIT or native factor authority. |
| QCF8 | From the required fresh origin, exercise BaoStock raw/factor/status/lifecycle and ratio-family fixtures with tradeable and suspended latest sessions. | The context carries the BaoStock lifecycle listing date and authoritative provenance before factory use; BaoStock alone establishes Current Tradeability; strict bundle identity requires its complete three-component binding; missing ratio families are requested narrowly; Tushare names and other sources cannot establish status. |
| QCF9 | From the required fresh origin, exercise Adjustment Factors and Name Events together, with Tushare factor success, Tushare name success, BaoStock factor success/failure, and Yahoo-derived terminal fallback. | The context is acquired once without seeded state; factor and name requests use distinct plan routes, endpoint scopes, keys, artifacts, and attempts while sharing one cache; name output remains descriptive; Yahoo factor output is `derived` and `degraded`; provider bundles are never blended. |
| QCF10 | From the required fresh origin, run complete, retryable-failure, terminal-failure, cooldown-skip, partial-fallback, and fresh-produced restored-cache matrices for every assembled provider. | Context and provider work share the one plan/router/coordinator. Every actual transport call has one and only one attempt event; retries and fallback match exact configured cardinality; cache hits, skips, normalization, selection, and restore add zero attempts; no hidden adapter retry or fan-out exists. |
| QCF11 | From the required fresh origin, concurrently issue identical first requests and request multiple periods/families/graph calls covered by one bulk artifact. | Concurrent first use performs one company-type resolution, one compatible listing acquisition, one context-level temporal evaluation transaction under one fixed `eligibility_evaluated_at`, one deterministic terminal decision binding, and one context bind; one leader performs each physical sequence, followers receive the same terminal result/decision, bulk payloads are acquired once, reuse counts are deterministic, and differing material keys do not collide. |
| QCF12 | Produce a checkpoint from a fresh built-in context acquisition and terminal qualified shadow request under plan 1.3, close all resources, reconstruct through the built-in factory, and repeat the exact request. | Context v2 identity/fields, temporal policy/calendar binding, fixed evaluation inputs, accepted/rejected decisions/results/reasons, plan, eligibility binding, field-semantics/metadata-compatibility versions, dispatcher state, cache entries, artifacts, candidates, typed rejections, selections, attempts, and IDs restore exactly; the repeat causes zero provider/network/model I/O, zero coordinator permits, and zero new attempts and does not consult today's calendar frontier. Missing, old/absent, contradictory, corrupt, or mismatched semantic/temporal/context/cache controls fail typed or require explicit `START_NEW` before work and do not repair or rewrite history. |
| QCF13 | Supply missing token/dependency, invalid mode, invalid plan/asset/context signature, invalid pacing/Safety Ceiling/account scope/Data Usage Mode/runtime root, incomplete source coverage including any of the six required executable bindings, and unsafe factory output. | Each pre-construction failure is secret-free and occurs before graph/model/provider work, store/artifact/report creation, or physical attempts. Typed context-acquisition failures occur only after allowed prerequisite work, leave the context slot unbound, and expose no partial callable composition. |
| QCF14 | Inject failure after each construction step, at every eligibility-metadata acquisition and context-binding step, during graph initialization, during provider dispatch, during graph execution, during checkpoint publication, and interrupt with `KeyboardInterrupt`. | The original failure remains primary; no partial context or callable dispatcher escapes; interrupt/close occur exactly once; new work stops; all sessions, BaoStock login, coordinator connections/store, leases, and workers are released or terminally abandoned; repeated close is harmless; no process-global mutable provider or eligibility state remains. |
| QCF15 | Put tokens, credential-like text, URLs with credentials, and raw provider exception text in fake environment values and transport failures. | Tokens and raw errors are absent from config, plan, repr, IDs, events, exceptions, logs, reports, checkpoints, artifacts, manifests, audits, and terminal output; only existing typed safe diagnostics cross boundaries. |
| QCF16 | Compare legacy output, qualified shadow output, daily market acquisition, Evidence Admission, Decision Gate, report, checkpoint, and decision audit before and after built-in registration. | Legacy-selected output and daily AKShare -> BaoStock -> Yahoo routing are unchanged; qualified output remains `shadow_non_authoritative`; no factory result changes admitted evidence or decision authority. |
| QCF17 | Open representative historical reports, checkpoints, artifacts, attempts, manifests, audits, snapshots, pins, and IDs—including the failed Ticket 07 run—then create a new built-in-factory run. | Historical material is byte-for-byte untouched, remains readable, and is interpreted only under its recorded semantic and temporal contracts; new qualified material uses the exact plan-1.3/context-v2 forward versions while unchanged raw artifact/candidate namespaces remain stable; no in-place migration, temporal reinterpretation, rewrite, deletion, or ID renumbering occurs. |
| QCF18 | Run PE1–PE15 and parent AC1–AC13 with their existing fixtures and expected results, followed by the full supported suite, Ruff, and whitespace/diff checks. | Every criterion passes unchanged and without waiver. QCF1–QCF18, PE1–PE15, AC1–AC13, and the full supported suite are run twice from the changed tree; any failure blocks the real rollout gate. |

### 06B deterministic temporal acceptance matrix

This matrix is additive acceptance under the existing QCF/PE/AC authority; it
does not renumber or replace QCF1–QCF18, PE1–PE15, or AC1–AC13. Every row runs
for fresh and retained company-type evidence and applicable listing/lifecycle
evidence through the one evaluator with a pinned calendar revision and fixed
`eligibility_evaluated_at`. `A/R` below means current-analysis accepted and
strict-PIT rejected; `R/R` means both rejected; `R/A` is the strict pre-cutoff
control. Boundary expectations are exact and session close is the frontier
transition instant.

| Scenario | Required current / strict result and proof |
| --- | --- |
| Requested session day, after session close and before evaluation | `A/R` when common provider proof passes; strict remains rejected for post-cutoff observation/retrieval. |
| Real next-calendar-day `00:48:07 Asia/Shanghai` retrieval for `2026-08-10` | `A/R` while `2026-08-10` is latest completed; proves local-date inequality has no authority. |
| Immediately before the next open session | `A/R`; the prior session remains latest completed. |
| During the next open session, before its close | `A/R`; session open does not advance the frontier. |
| At and immediately after the next session close | `R/R` for a new prior-session decision; the later session is now latest completed. |
| Friday retrieval on Saturday | `A/R` when Friday remains latest completed. |
| Friday retrieval on Sunday | `A/R` when Friday remains latest completed. |
| Pre-holiday session retrieved during the mainland holiday | `A/R` throughout the non-session interval and until the next open session completes. |
| Retained evidence reused after one or more later completed sessions | `R/R` for a new run; an earlier acceptance cannot be rebound to an obsolete frontier. Exact replay of the original run remains unchanged. |
| Provider `ann_date` after `requested_session` | `R/R`. |
| Eligible `ann_date` but missing or later `f_ann_date` | Current may be accepted; strict is rejected. |
| Exact real `00:48` artifact in current-only versus strict mode | Current may be accepted with complete provider-derived proof; strict remains rejected unless the independent exact-cutoff predicate passes. |
| Direct pre-cutoff strict control | `R/A` when `f_ann_date` is eligible and both immutable observation/retrieval timestamps are no later than `strict_cutoff`. |
| Qualified exact-revision availability control | Strict accepts a post-cutoff retrieval only when `exact_revision_authoritative_available_at <= strict_cutoff` is authoritative and binds the exact revision; generic filing availability rejects. |
| Missing provider-derived `evidence_effective_as_of` or missing provider `ann_date` | `R/R`; no request-date, retrieval-date, route, name, ticker, factory, or model backfill. |
| Replay of every accepted and rejected row | The original decision identity, inputs, result, and reasons reproduce exactly with zero provider/network/model I/O and no consultation of the restore-time calendar frontier. |
| Thirty-two concurrent followers on first acquisition | One leader performs acquisition and exactly one context-level temporal evaluation transaction; evaluator count is one, all followers receive one immutable terminal decision/context, and no clock race occurs. |

06B also reruns the complete Ticket 06 acceptance—QCF1–QCF18, PE1–PE15,
AC1–AC13, non-integration/full suites, Ruff, diff/whitespace, secret/history
checks, and two-stage review—twice from the same changed tree. Targeted temporal
tests cannot by themselves re-authorize Ticket 07.

### Final real two-symbol shadow rollout gate

This is the only live-provider step and is not run while writing or implementing
the deterministic tests. It requires explicit operator authorization after
02G, 02H, 05C, 06B, the complete Ticket 06 rerun, and all QCF1–QCF18 pass.
Ticket 07 remains **BLOCKED**. The failed `601328.SS` run for requested session
`2026-08-10`, including the Tushare artifact retrieved at
`2026-08-10T16:48:07Z`, remains historical and immutable and cannot be resumed,
repaired, relabeled, or reused as the retry namespace.

1. Use the shipped built-in registration with no direct composition and no
   explicit operator factory. Use the existing qualified profile, Capability
   Routing Plan, environment credential boundary, Data Usage Mode, Safety
   Ceilings, completeness thresholds, and provider routes.
2. Use a newly allocated run ID and checkpoint namespace under plan 1.3/context
   v2 and retained content-addressed artifacts only when they independently pass
   the new temporal evaluator. Do not clear or reuse historical reports,
   checkpoints, attempts, decisions, or IDs to manufacture a clean result.
3. Run one symbol at a time for the same latest completed mainland session:
   `601328.SS` first as the bank case. Run `600895.SS` as the non-bank case only
   after the new `601328.SS` run passes every gate. Invoke explicit
   `qualified_v1_shadow` validation through the real CLI seam; a failure of the
   bank case ends the retry without calling the second symbol.
4. For each symbol, retain the safe preflight result, Capability Routing Plan
   signature, Financial Eligibility Acquisition Context identity/projection,
   dispatcher/cache checkpoint projection, provider attempt events, manifest,
   report, and decision audit. Do not retain or print credentials or raw
   provider errors outside existing immutable provider-artifact boundaries.
5. The gate passes only if both runs avoid
   `qualified_routing_composition_missing`, complete through the registered
   production composition, originate and bind one eligibility context without
   seeded state, record one plan-1.3 temporal evaluation/binding with provider-
   derived effective/publication proof and the pinned latest-completed-session
   frontier, route all physical calls through the coordinator, establish
   BaoStock Current Tradeability authority, retain and truthfully assess
   Tushare indicator artifacts, continue only metadata-unresolved indicator
   families through the plan, show exact/non-duplicated attempts, and publish
   report plus decision audit as
   `shadow_non_authoritative` while legacy-selected output remains authoritative.
6. A typed provider/configuration/temporal failure, missing status authority,
   unexplained attempt, secret/raw-error exposure, authority drift, historical-
   state mutation, or failure of either symbol blocks the gate. Every later
   retry must use another new run identity, start again with `601328.SS`, and
   must not rewrite any failed run.
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
| **02E — Financial-indicator semantic metadata contracts** | Add `financial-indicator-field-semantics-v1`, the complete closed classification registry, applicability/proof states, `financial-indicator-metadata-compatibility-v1`, assessment identity/typed reasons, plan 1.2 bindings, and the unchanged completeness/current/PIT boundaries. Keep raw candidate/period identities at v1 and reject inferred companion-filing lineage. No provider implementation or live I/O. | Existing **01** and the accepted QCF4 metadata-compatibility prototype report. | 02F, 05B, 06A, and Ticket 06. |
| **02F — Truthful provider indicator normalization and compatibility evaluation** | Consume 02E. Write AKShare indicator normalizer v2 and BaoStock ratio normalizer v2 without request metadata backfill; add versioned Tushare/BaoStock result projections only where needed to carry assessments; evaluate Tushare, AKShare, BaoStock, and Yahoo through one provider-neutral compatibility seam; retain incompatible candidates and return only unresolved families to the existing router. No route or Evidence Admission changes. | 02E. | 05B, 06A, and Ticket 06. |
| **05B — Persist and restore indicator metadata compatibility** | Add indicator manifest v3, indicator routing-result v3, provider-candidate/selection v2/v3 bindings, indicator dispatch/checkpoint v2, dispatch ledger 1.5, indicator completeness/aggregate audit rows v4/v3, indicator manifest/request audit v3, Financial Dispatch Audit Projection 3.0, tool-manifest v3, and tool-message envelope v3 while statement projections remain v2. Preserve historical readers, exact pinned-contract replay, typed/`START_NEW` rejection of old compatibility checkpoints under plan 1.2, and zero-I/O/no-repair restore. | 02E, 02F, and 05A. | 06A and Ticket 06. |
| **06A — Correct QCF4 deterministic acceptance** | Replace only the over-specified QCF4 expectation with the fresh-origin statement-success plus retained/rejected-indicator plus plan-owned lower-provider fallback behavior above. Trace both fixed symbols; prove no request metadata is provider evidence and prove exact prerequisite/ordinary attempt cardinality. Retain the separately completed deterministic live-test isolation fix and rerun QCF1–QCF18, PE1–PE15, and AC1–AC13 unchanged outside corrected QCF4. | 02F, 05B, and all existing Ticket 06 prerequisites. | Ticket 06; QCF5–QCF12 cannot proceed until corrected QCF4 passes. |
| **02G — Temporal eligibility contracts/evaluator** | Close the temporal terminology; add provider-neutral `evidence_effective_as_of`, `provider-neutral-temporal-eligibility-v1`, `market-session-eligibility-binding-v1`, the decision projection/identity, current-analysis session-frontier predicate, strict-PIT predicate, exact reason taxonomy, and plan 1.3/context-v2 forward bindings. Preserve `ann_date`/`f_ann_date` and immutable timestamps. No provider implementation, live I/O, grace window, or local-date substitution. | 02A and ADR-0036. | 02H, 05C, 06B, Ticket 06, and Ticket 07. |
| **02H — Temporal proof projection/acquisition integration** | Project only the exact provider-origin effective/publication derivations listed above for fresh and retained company-type/listing metadata; replace all exact-local-date authorities with the single evaluator; bind one fixed evaluation context through acquisition, context construction, request binding, and relevant listing/lifecycle seams; fail closed without request-date backfill. Preserve provider routes and single-flight ownership. | 02G, 02B, and 02D. | 05C, 06B, Ticket 06, and Ticket 07. |
| **05C — Temporal checkpoint/replay compatibility** | Persist policy/calendar versions, requested session/mode, fixed evaluation inputs, provider proof/timestamps, latest-completed binding, strict proof, accepted/rejected result/reasons, and decision identity in context v2, cache checkpoint 1.2, ledger 1.6, manifest v4, and exact downstream projections. Reproduce exact decisions with zero provider/model I/O and no restore-time frontier lookup. Plan-1.2/context-v1 under plan 1.3 fails typed or requires explicit `START_NEW`; no migration or historical reinterpretation. | 02G, 02H, 05A, and 05B. | 06B, Ticket 06, and Ticket 07. |
| **06B — Deterministic temporal acceptance** | Run every temporal matrix above: next-day 00:48, before/during next session, next-session close, weekend, holiday, stale reuse, announcement/effective-proof failures, strict cutoff/exact revision proof, zero-I/O every-case replay, and concurrent first acquisition with one fixed evaluation. Then rerun complete QCF1–QCF18, PE1–PE15, AC1–AC13, non-integration/full suites, Ruff, history/secret/diff checks, and two-stage review twice. No live providers or paid models. | 02H, 05C, 06A, and all existing Ticket 06 prerequisites. | Ticket 06 and Ticket 07. |

The exact dependency graph is:

```text
01 -> 02, 03, 04, 05, 02A
02A + 02 -> 02B
02A -> 02C
02A + 02 -> 02D
02A + 02B + 02C + 02D + 04 -> 04A
02A + 02B + 02C + 04A + 05 -> 05A
01 + QCF4 prototype report -> 02E
02E -> 02F
02E + 02F + 05A -> 05B
02A + ADR-0036 -> 02G
02G + 02B + 02D -> 02H
02G + 02H + 05A + 05B -> 05C
03 + 02D + 04A + 02F + 05B -> 06A
02H + 05C + 06A + all existing Ticket 06 prerequisites -> 06B
03 + 02D + 04A + 05B + 06A + 06B -> 06
06 + 06B -> 07
```

The mandatory temporal chain inside that integrated graph is exactly:

```text
02G -> 02H -> 05C -> 06B -> Ticket 07
```

Existing Ticket 03 remains the static-selection/caller-precedence slice and may
proceed after Ticket 01; it does not own context construction. Existing Ticket
04 remains the resource-lifecycle prerequisite to 04A. Existing Ticket 05
remains the cache/dispatcher restoration prerequisite to 05A. Ticket 06 remains
**FAIL/BLOCKED** even if its original Tickets 02–05 are marked complete: it
cannot complete until 02A–02H, 04A, 05A–05C, 06A, and 06B are complete and their
deterministic tests pass twice as specified. Within Ticket 06, QCF5–QCF12 remain
blocked behind the corrected QCF4 path. Ticket 07 remains blocked by 06B,
completed Ticket 06, and fresh explicit operator authorization for live-provider
work.

## Out of Scope

- Enabling authoritative `qualified_v1`, changing its activation gate, or
  recommending activation.
- Changing any existing ordinary provider route/order, fallback policy,
  completeness threshold, financial eligibility rule, revision-selection rule,
  or qualification profile. The new versioned prerequisite company-type-
  evidence capability is the sole additive route defined by this amendment.
- Broadly rewriting or weakening Evidence Admission, or changing Decision Gate,
  Strategy Rules, Validated Decision Context, canonical Evidence/Source Fact
  versions, or the authority of legacy-selected output. The new provider-neutral
  compatibility assessment is completed before selection at the existing trust
  boundary.
- Changing BaoStock status, lifecycle, or strict-history authority.
- Changing the daily mainland AKShare -> BaoStock -> Yahoo market route or adding
  Tushare to daily market acquisition.
- Adding speculative prewarming, parallel provider fan-out, provider probing,
  account rotation, hidden retries, or future-replay work to foreground analysis.
- Introducing a new provider cache, checkpoint sidecar, competing report/audit
  authority, process-global provider registry, or process-global eligibility
  context. Only the exact forward contract/identity versions listed above are
  permitted.
- Rewriting, deleting, migrating in place, or relabeling any historical report,
  checkpoint, artifact, attempt, manifest, audit, snapshot, pin, provider row, or
  ID.
- Moving credentials into config, files, command arguments, prompts, logs, or
  persisted state.
- Running live providers or paid models while amending or implementing this
  deterministic contract. The separately authorized future two-symbol gate is
  outside this amendment and remains blocked by Ticket 06.
- Running the blocked Ticket 07 retry, mutating its failed `601328.SS` run, or
  treating an amended spec as rollout authorization.
- Using exact retrieval-local-date equality, a next-calendar-day or elapsed
  grace window, next-session open, or a simple `<=`/`>=` date substitution as a
  replacement temporal policy.
- Backfilling `evidence_effective_as_of`, `ann_date`, `f_ann_date`, observation,
  retrieval, or exact-revision availability from request intent, retrieval
  convenience, provider route, factory defaults, symbol/name, or model output.
- Copying either the fresh-run or QCF4 metadata-compatibility prototype into
  production, importing prototype code at runtime, or treating prototype
  code/tests as implementation completion. Their reports are design evidence;
  production work uses the existing contracts and seams named here.
- Copying the temporal-eligibility prototype implementation into production or
  treating its successful matrices as implementation/rollout completion. Its
  report is authoritative design evidence only; 02G–06B must implement through
  the existing production seams.

## Further Notes

- ADR-0031 requires every physical call to use the centralized Provider Request
  Coordinator, with sequential fallback, shared cooldowns, exact attempt
  accounting, and no fan-out. This specification is additive and consistent
  with that decision.
- The separately completed deterministic live-test isolation fix is retained as
  prior state. This amendment neither reopens nor replaces it, and implementation
  must not weaken its provider/model isolation guarantees.
- ADR-0032 continues to govern the operator’s Data Usage Mode. A shipped factory
  does not imply commercial provider entitlement.
- ADR-0033 and ADR-0034 preserve complete BaoStock Provider History Bundle and
  strict-history authority. ADR-0035 prevents the factory from adding foreground
  work for replay. ADR-0036 and ADR-0037 preserve Market Session Calendar and
  History Gap behavior.
- ADR-0036 is the authority for the current-analysis frontier. The calendar
  decides session completion while provider artifacts decide material
  effective/publication proof. This resolves the Ticket 07 midnight failure
  without weakening strict PIT or inventing a grace interval.
- The highest seams are fixed: host builder for static selection; graph/run host
  for context lifetime and single assignment; provider-neutral acquisition over
  the one plan/router for metadata construction; and the checkpoint-aware
  financial tool/dispatcher boundary for request binding/restoration. The
  factory may add lifecycle capabilities to the managed composition contract,
  but it must not add another routing, eligibility, or persistence authority.
- `Status: ready-for-agent` is justified because fresh-run ownership, metadata
  applicability semantics, current-analysis latest-completed frontier, strict-
  PIT cutoff proof, provider-neutral `evidence_effective_as_of` provenance,
  calendar/weekend/holiday/session boundaries, one evaluator seam, exact
  forward versions, checkpoint/replay compatibility, fixed-time concurrency,
  Ticket 07 retry order, the complete field classification, unchanged Evidence
  Admission boundary, corrected QCF4 expectation, plan-owned fallback, provider
  source wiring, lifecycle behavior, and integrated work dependencies are fully
  resolved above. Ticket 06 nevertheless remains **FAIL/BLOCKED** until
  02A–02H, 04A, 05A–05C, 06A, and 06B are implemented and the complete
  deterministic acceptance passes twice; Ticket 07 remains **BLOCKED** and its
  failed run immutable. Any implementation that cannot satisfy one of these
  decisions must return the specification to `needs-info` rather than inventing
  a second owner or weakening a trust boundary.

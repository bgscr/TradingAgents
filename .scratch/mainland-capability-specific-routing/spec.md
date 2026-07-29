# Capability-specific mainland equity data routing

Status: ready-for-agent

## Problem Statement

TradingAgents currently applies provider precedence too broadly for mainland
Equities. Daily market history, financial statements, financial indicators,
adjustment factors, trading status, and issuer lifecycle data have different
qualified providers, schemas, completeness characteristics, lineage, and
failure modes. A single category-wide provider choice can therefore discard a
usable statement period because another subrequest failed, accept a ratio proxy
as if it were a full statement, lose bank-specific fields, or use a provider for
a status claim that its qualified endpoint cannot establish.

Operators need the system to acquire each capability through its own ordered,
qualified route, normalize provider artifacts into explicit company-type and
period contracts, retain every artifact and typed outcome, and select only
compatible periods that pass deterministic completeness gates. They also need
the existing Current Analysis Provider Chain, Provider History Bundle rules,
canonical Evidence Admission, Decision Gate, reports, checkpoints, and strict
replay guarantees to remain intact. More acquired data must improve observable
coverage without automatically becoming a Source Fact or weakening a
fail-closed Trading Decision boundary.

## Solution

Introduce one versioned mainland Capability Routing Plan resolved before graph
or model work. The plan records capability-specific provider order, feature
enablement, qualification profile, normalization policy, request budget, and
artifact/manifest contract identity without storing a credential. It preserves
the existing AKShare -> BaoStock -> Yahoo daily market-snapshot order and adds
independent routes for statements, indicators, factors, status, and issuer
history.

Evolve the existing run-scoped financial dispatcher into the provider-neutral
selection seam for financial capabilities. Providers return immutable provider
artifacts plus normalized period candidates rather than one opaque winning
string. The dispatcher evaluates each candidate per capability, statement,
company type, scope, unit, currency, and period; retains accepted and rejected
artifacts; accepts qualifying periods in deterministic route order; and
continues fallback only for missing, conflicted, or rejected periods. A failure
in one provider, statement, ratio family, or period does not erase already
accepted independent periods.

All physical provider requests continue through the Provider Request
Coordinator under one Upstream Service Identity per real upstream/account
boundary. Tushare uses endpoint-aware request identity and pacing while
remaining one shared account-scoped upstream. The existing public financial
tool names, market-snapshot semantics, Source Fact admission, Evidence
Preflight, Evidence Admission, Decision Gate, terminal contracts, and Strategy
Rules remain unchanged.

## User Stories

1. As a mainland Equity user, I want daily market snapshots to keep the existing AKShare, BaoStock, then Yahoo order, so that financial-data improvements do not change exact market-price semantics.
2. As a mainland Equity user, I want Tushare used only for capabilities it passed, so that an account upgrade does not silently make it the global provider.
3. As a fundamental analyst, I want balance sheets routed through Tushare, AKShare-Sina, then Yahoo, so that I receive the best qualified coverage for each period.
4. As a fundamental analyst, I want income statements routed independently of balance sheets and cash flows, so that one statement failure does not discard another usable statement.
5. As a fundamental analyst, I want cash-flow statements routed independently per period, so that a sparse or failed subrequest does not erase usable history.
6. As a fundamental analyst, I want five usable annual and eight usable reporting periods when the issuer has enough listed history, so that time-series analysis has a deterministic coverage target.
7. As a recent-listing issuer user, I want an explicit since-listing exception based on authoritative listing metadata, so that the system does not invent unavailable pre-listing coverage or silently weaken the normal gate.
8. As a bank investor, I want bank statements evaluated against a bank core rather than an industrial-company union schema, so that taxonomy sparsity is not mistaken for missing evidence.
9. As a non-bank investor, I want non-bank critical and core fields evaluated separately from bank-only fields, so that irrelevant columns do not distort completeness.
10. As an audit reader, I want original and normalized values, units, currency, scope, company type, and period identity preserved together, so that provider transformations remain reviewable.
11. As a point-in-time researcher, I want `ann_date` and `f_ann_date` preserved independently, so that announcement and first-publication semantics are never conflated.
12. As a point-in-time researcher, I want strict no-lookahead claims gated by first-publication date, so that a historical report date cannot make later information available early.
13. As an audit reader, I want `report_type`, `comp_type`, and `update_flag` retained, so that statement scope, issuer taxonomy, and provider update state remain visible.
14. As an audit reader, I want a stable local provider-revision identity even when a provider exposes only a binary update flag, so that changed values cannot overwrite earlier observed artifacts.
15. As a financial-indicator user, I want Tushare tried before AKShare and qualified BaoStock ratio families, so that current analysis uses the strongest measured coverage.
16. As a point-in-time researcher, I want Tushare indicators treated as current-analysis capable until separate first-publication lineage is proven, so that missing lineage is not overstated.
17. As a ratio user, I want BaoStock profit, operation, growth, balance, cash-flow, and DuPont families requested only when higher-priority normalized fields remain missing, so that the system avoids unnecessary physical calls.
18. As a bank investor, I want unusable bank operation or cash-flow ratio families rejected without discarding usable bank growth or DuPont families, so that fallback is family-specific.
19. As a statement user, I want ratio-family payloads categorically unable to satisfy a full-statement request, so that partial metrics cannot masquerade as a balance sheet, income statement, or cash flow.
20. As a market-history user, I want BaoStock adjustment factors to remain first, so that the existing complete Provider History Bundle and Strict History Provider remain intact.
21. As a current-analysis user, I want qualified Tushare and AKShare native factors available after BaoStock, so that factor coverage can improve without blending strict bundles.
22. As an audit reader, I want Yahoo-derived factors labeled degraded and derived, so that a calculated ratio is not presented as a native Adjustment Factor Revision.
23. As a trader, I want BaoStock trading status to remain authoritative, so that current tradeability is based on qualified per-session status.
24. As a trader, I want empty Tushare `suspend_d` results treated only as supplemental event outcomes, so that absence of an event is not promoted to authoritative tradeability.
25. As a risk owner, I want the existing confirmed-suspension Decision Gate behavior unchanged, so that more issuer data cannot permit a directional decision for a non-tradeable Instrument.
26. As an issuer-history user, I want name events routed through Tushare and then AKShare, so that dated name coverage improves.
27. As a risk owner, I want ST state derived only from authoritative dated status evidence, so that an `ST`-looking company name cannot create a trading-status fact.
28. As an issuer-history user, I want IPO, delisting, dated ST, and trading-status metadata sourced BaoStock-first, so that lifecycle semantics remain bound to the qualified status provider.
29. As an operator, I want every physical Tushare, AKShare, BaoStock, and Yahoo request coordinated and counted, so that one logical request cannot hide multiple network calls.
30. As an operator, I want identical endpoint/symbol/period requests single-flighted and reused within the run and checkpoint, so that qualified single-stock interfaces are not called redundantly.
31. As an operator, I want shared endpoint and account cooldowns to survive process boundaries, so that a Tushare throttle in one process protects other callers.
32. As an operator, I want permission, authentication, rate-limit, empty, malformed, completeness, and metadata failures typed separately, so that fallback and diagnostics remain deterministic.
33. As an operator, I want credentials absent from logs, errors, reports, checkpoints, manifests, and audits, so that enabling Tushare does not expand secret exposure.
34. As a model-risk owner, I want all acquired artifacts retained independently of model citations, so that uncited evidence is not lost and cited prose does not define operational lineage.
35. As a model-risk owner, I want acquisition to remain separate from Source Fact construction, so that broader provider coverage does not automatically influence a Trading Decision.
36. As an operator, I want hard-gate failures to continue fallback and end as Degraded Evidence or Insufficient Evidence when no candidate qualifies, so that the system never silently fills gaps.
37. As an operator, I want incompatible currency, unit, scope, company type, period, or critical values rejected or marked conflicted, so that cross-provider selection cannot create a false composite.
38. As an existing user without Tushare financial enablement, I want the current fallback routes and output contracts preserved, so that the upgrade is opt-in and reversible.
39. As a checkpoint user, I want old checkpoints and reports readable without rewriting while incompatible resumptions fail typed, so that historical audit material remains immutable.
40. As a maintainer, I want deterministic fixtures for bank, non-bank, Shanghai, Shenzhen, and recent-listing cases, so that implementation does not depend on live provider availability.
41. As a maintainer, I want parent AC1-AC13 rerun unchanged, so that capability routing cannot regress market-data safety, point-in-time correctness, retry accounting, migration safety, or worktree hygiene.
42. As a maintainer, I want a feature-flag rollback to the legacy plan without data migration or artifact deletion, so that rollout can stop immediately if compatibility diverges.

## Implementation Decisions

### Authoritative qualification evidence

The July 2026 provider qualification and Tushare 2,000-point requalification are
accepted inputs, not work to repeat. Implementation must encode their decisions:

- Tushare statements are capability-qualified only behind company-type
  normalization, completeness, first-publication, and revision gates.
- Tushare financial indicators are current-analysis capable and do not establish
  strict point-in-time lineage by themselves.
- Tushare native factors are qualified as a factor capability, not as a complete
  Provider History Bundle.
- Tushare `suspend_d` is not authoritative suspension/status history.
- Tushare `namechange` qualifies dated name events, not dated ST status.
- BaoStock remains authoritative for raw observations, factors, session status,
  dated `isST`, and listing metadata, and its financial endpoints are ratio
  families rather than full statements.
- The tested AKShare upgrade produced no measured improvement; dependency
  upgrade is not part of this work.

No live provider qualification, raw provider payload, credential, or token is a
deliverable of this specification.

### Cross-cutting invariants

- Preserve the Current Analysis Provider Chain for daily mainland snapshots:
  AKShare -> BaoStock -> Yahoo.
- Preserve one complete provider frame for an Authoritative Market Snapshot and
  one complete Provider History Bundle for strict reconstruction. Per-period
  financial selection does not authorize market-row or strict-bundle blending.
- Preserve BaoStock as the initial Strict History Provider.
- Preserve sequential fallback, single-flight, shared cooldowns, exact physical
  attempt accounting, foreground priority, and Operator Safety Ceilings.
- Preserve Evidence Preflight, Evidence Admission, Validated Decision Context,
  Decision Gate, Strategy Rules, and terminal outcome semantics.
- Acquisition availability and completeness are not Source Facts. Only a
  separately validated projection may construct canonical facts from selected
  periods.
- Preserve every runtime artifact and manifest immutably; never rewrite legacy
  checkpoints, reports, provider records, snapshot IDs, pins, or payloads.
- Never silently normalize unknown units, convert currencies, infer company
  type from a name, infer ST state from a name, infer consolidation scope, or
  substitute announcement date for first-publication date.
- All new contract identities are deterministic, versioned, canonical, and
  independent of model prose, process ID, call order, or row order.

### Highest implementation and testing seam

Use the existing run-scoped financial dispatcher and compiled financial tool
node as the single logical seam for statement and indicator acquisition. They
already own canonical request keys, immutable provider plans, duplicate
suppression, typed Source Acquisition Outcomes, checkpoint ledgers, audit
projection, and tool-message correlation. Do not add a second financial router.

Keep the Provider Request Coordinator as the only physical-network seam. Each
adapter exposes physical subrequests to the dispatcher/coordinator; adapter SDK
calls cannot perform hidden retries or bypass attempt recording.

Keep the Authoritative Market Snapshot boundary as the market/factor/status seam
and keep the existing evidence envelope and admission boundaries as the only
route into Evidence State and Source Facts.

### Immutable Capability Routing Plan and feature configuration

Resolve a closed, versioned `qualified_v1` mainland Capability Routing Plan
after authoritative Instrument Identity and before graph construction. Its safe
projection is stored in run state, checkpoints, audit, and the financial request
key. It contains:

- policy version and deterministic plan signature;
- ordered provider IDs for each capability;
- enabled Tushare capability IDs;
- qualification profile ID `cn-a-2000-20260729-v1`;
- normalizer and completeness-policy versions;
- non-secret Tushare account-scope label;
- endpoint pacing policy identities and foreground request budgets;
- artifact, manifest, period-selection, and degradation contract versions.

The plan contains no token, token digest, credential text, provider exception
text, or raw payload.

Configuration is additive and closed:

- A master mainland capability-routing mode defaults to `legacy`.
- `qualified_v1` is opt-in.
- Tushare enablement is a closed set containing only `statements`,
  `financial_indicators`, `adjustment_factors`, and `name_events`.
- `suspension_status` is not a valid Tushare enablement in this profile.
- Any non-empty Tushare enablement requires the exact qualification profile,
  a non-empty token available only through the existing environment boundary,
  the optional Tushare dependency, `personal_research` Data Usage Mode, and a
  valid configured Operator Safety Ceiling.
- Missing or contradictory enabled/token/profile/mode/dependency/pacing state
  produces a typed preflight Analysis Outcome before any model call.
- Existing configuration with `legacy` mode or no Tushare capability list keeps
  the current registered provider chains and output behavior.
- The immutable plan, not later environment changes, controls a resumed run.

### Production routing matrix

| Capability | Ordered route | Selection boundary | Authority/degradation rule |
| --- | --- | --- | --- |
| Daily market snapshot | AKShare -> BaoStock -> Yahoo | Complete validated provider frame | Unchanged; no Tushare |
| Balance sheet | Tushare -> AKShare-Sina -> Yahoo | Normalized statement period | Yahoo without required metadata is degraded/current-only |
| Income statement | Tushare -> AKShare-Sina -> Yahoo | Normalized statement period | Same |
| Cash flow | Tushare -> AKShare-Sina -> Yahoo | Normalized statement period | Same |
| Financial indicators | Tushare -> AKShare -> qualified missing BaoStock families -> Yahoo | Normalized indicator field/family and period | Tushare current-analysis only unless PIT binding exists; Yahoo current snapshot only |
| Adjustment factors | BaoStock -> Tushare -> AKShare -> Yahoo-derived | Native dated factor dataset; strict fallback remains bundle-atomic | Yahoo is degraded/derived; a factor alone is not a strict bundle |
| Suspension/trading status | BaoStock authoritative | Per-session status in provider bundle | Tushare event artifacts are supplemental and non-authoritative |
| Name events | Tushare -> AKShare | Dated name event | Never establishes ST status |
| Dated ST, IPO, delisting, listing | BaoStock first | Dated status/lifecycle observation | No inference from names |

### Work area A — Provider-neutral financial statement and completeness contracts

Define closed versioned contracts for:

- financial capability, statement type, reporting frequency, and ratio family;
- canonical Financial Company Type: bank, industrial/non-bank, insurance,
  securities, other-financial, and unknown;
- provider artifact identity and provider dataset identity;
- canonical Financial Period Identity containing canonical Instrument Identity,
  statement or ratio capability, period end, frequency, company type,
  consolidation scope, currency, and provider revision binding;
- original provider field/value/unit and normalized field/value/unit;
- filing metadata containing `ann_date`, `f_ann_date`, `report_type`,
  `comp_type`, `update_flag`, retrieval time, and stable local revision identity;
- per-period field coverage, critical/core coverage, metadata coverage, and
  disposition;
- typed since-listing exception with authoritative listing date and provenance;
- financial acquisition manifest, provider artifact entries, period candidates,
  period selections, rejected-period reasons, overlap/conflict findings, and
  aggregate completeness assessment.

Provider artifacts are immutable content-addressed runtime artifacts. The local
provider revision identity commits to provider, endpoint/dataset, canonical
request, provider metadata, retrieved/observed time, schema identity, and payload
digest. `update_flag` is preserved but is not treated as a globally unique
restatement identifier.

Company type resolution is deterministic and evidence-bearing:

1. Use a qualified provider-declared company type when present.
2. Otherwise use a versioned adapter classifier based on mutually exclusive
   provider field signatures and explicit provider industry/type metadata.
3. Record the classifier version and inputs.
4. Reject unknown, ambiguous, or cross-provider contradictory classifications
   for normalized selection; preserve their artifacts operationally.

Each company type has a declared critical field set and qualified core field
set per statement and ratio family. Implementations must not dynamically define
the production core as “fields populated in this response.”

Completeness rules:

- Every selected statement period has 100% of critical company-type fields and
  at least 90% of its declared company-type core.
- Normal issuers target the latest five eligible annual periods and the latest
  eight distinct reporting periods at quarterly cadence, including year-end
  periods.
- A since-listing exception is allowed only when authoritative listing age makes
  the normal target impossible. It covers every expected statement period whose
  period end is on or after listing and records a typed exception. Pre-listing
  rows do not satisfy the exception.
- Ratio families require eight usable reporting periods and no more than 10%
  missingness in the declared normalized family set.
- `ann_date` is required for an accepted financial period.
- `f_ann_date` is required before a strict no-lookahead claim. A current-only
  period may be retained and rendered as degraded when it lacks `f_ann_date`,
  but it cannot enter strict historical replay or support an as-of claim before
  publication.
- Restatement lineage may be claimed only when a stable provider revision or
  provider filing/update identity distinguishes revisions. A binary
  `update_flag` alone is insufficient; local payload revision identity remains
  operational provenance, not proof of the provider's restatement chronology.

Add typed unavailable/rejection reasons for permission denied, insufficient
completeness, incompatible metadata, capability mismatch, missing first
publication for strict PIT, unstable revision identity, conflicting critical
values, and unsupported since-listing coverage. Existing typed rate-limit,
authentication, no-data, malformed, usage-entitlement, and upstream failures
remain distinct.

### Work area B — Tushare statement and indicator adapters

Implement single-stock adapters for `income`, `balancesheet`, `cashflow`,
`fina_indicator`, `adj_factor`, and `namechange`. Do not use VIP cross-sectional
interfaces.

The statement adapters:

- request the qualified single-stock history range once per exact
  endpoint/symbol/as-of request and project annual and quarterly/reporting views
  from that immutable artifact;
- preserve all original fields and the required filing metadata;
- map `comp_type` through the declared Financial Company Type mapping;
- map `report_type` to a declared consolidation/report scope and reject unknown
  or incompatible scope for hard-gated selection;
- preserve original values and provider units, normalize monetary values to
  explicit CNY base units only when the endpoint contract makes the scale
  unambiguous, and reject silent scale inference;
- retain duplicate period rows as revisions/candidates until deterministic
  filing/revision selection completes;
- sanitize all SDK failures before they enter typed outcomes; never include the
  token or unbounded provider exception text.

The indicator adapter emits only the financial-indicator capability. It
preserves `ann_date`, records the absence of `f_ann_date` and update/scope fields,
and marks its periods current-analysis capable unless a separate admitted
statement binding proves first publication.

The factor adapter emits native dated Adjustment Factor Revisions but does not
claim raw/status bundle completeness. The name adapter emits dated name events
only. This work does not add a decision-authoritative Tushare suspension adapter;
any existing `suspend_d` ingestion remains supplemental and outside Current
Tradeability authority.

Tushare physical calls use one account-scoped `tushare-pro` Upstream Service
Identity. Endpoint is a capacity/cache scope, not a separate upstream identity.
The existing conservative default of 40 calls/minute is retained; configured
endpoint overrides must be positive and cannot exceed the provider-published
2,000-point ceiling of 200 calls/minute. Prewarming remains disabled by default.

### Work area C — AKShare-Sina statement adapters and partial-result salvage

Replace mainland statement placeholders with real AKShare-Sina adapters for
balance sheet, income statement, and cash flow. Preserve the qualified current
AKShare package version.

Each endpoint/subrequest returns an independent provider artifact. The adapter
must:

- use the real Sina financial-report endpoint for the requested statement;
- preserve report date, announcement date, currency, type/scope metadata, and
  update timestamp/field when supplied;
- classify and normalize bank and non-bank schemas through the contracts from
  work area A;
- emit an explicit absence for first-publication or stable restatement identity
  rather than substituting announcement/update dates;
- return usable periods even when another statement endpoint, frequency view,
  or supplemental subrequest fails;
- expose each actual remote request through the Provider Request Coordinator;
- avoid calling Yahoo internally. Yahoo remains a dispatcher-level fallback so
  its attempts and artifacts are visible in the common manifest.

AKShare financial indicators remain a distinct adapter/capability and cannot be
used to satisfy statement fields unless a declared normalized indicator field
is explicitly requested.

### Work area D — BaoStock factor, status, ratio, and lifecycle adapters

Expose qualified BaoStock capabilities explicitly:

- raw daily observations;
- forward adjustment factors;
- per-session `tradestatus`;
- dated `isST`;
- IPO, listing status, and out/delisting metadata;
- profit, operation, growth, balance-ratio, cash-flow-ratio, and DuPont families.

The existing complete BaoStock history bundle remains the authoritative strict
boundary for raw observations, factors, and status. Ratio adapters are separate
financial-indicator families. Each symbol/year/quarter request is a distinct
coordinated physical attempt and a distinct cache key. Only families/periods
still missing after Tushare and AKShare are requested. A usable result in one
family survives failure or sparsity in another.

BaoStock ratio artifacts always carry capability `financial_ratio_family` plus
their exact family. Passing them to a full-statement request yields typed
`capability_mismatch` and continues statement fallback. Bank operation and
cash-flow families must pass the same declared 10% normalized missingness gate;
qualification evidence does not permit a bank-specific waiver.

### Work area E — Capability routing and normalized period selection

Extend the financial dispatcher from whole-response first-success behavior to a
deterministic candidate/selection plan while preserving its public tool-call
contracts, canonical correlation, single-flight, and checkpoint ownership.

For each logical statement request:

1. Resolve the immutable route from the run plan.
2. Query the first enabled provider through an endpoint-aware subrequest cache.
3. Persist the provider artifact and manifest entry before evaluating periods.
4. Normalize each returned period and record accepted or rejected disposition.
5. Select a period only if capability, statement type, company type, unit,
   currency, scope, period identity, filing metadata, and completeness gates
   pass.
6. Preserve accepted periods even if another period or subrequest fails.
7. Continue sequential fallback for target periods that remain missing,
   rejected, or conflicted. Do not request a later provider solely to duplicate
   already complete target coverage.
8. When a later provider response necessarily contains overlapping periods,
   retain them as unselected candidates. Equivalent normalized values confirm
   compatibility; a critical-value conflict marks only the affected period
   conflicted and prevents its Source Facts until resolved.
9. Never coerce or combine incompatible unit, currency, scope, company type, or
   period identities. Record typed rejection and continue fallback.
10. Produce one deterministic selection manifest and model-facing rendering from
    selected periods after the route terminates.

The public balance-sheet, income-statement, and cash-flow tools remain. For
mainland Equities, the existing fundamentals tool remains the public seam for a
composite issuer overview; its financial-indicator section uses the indicator
route rather than adding a second model-selected tool. Existing non-mainland
tool behavior remains unchanged.

Yahoo statements may be retained and rendered as final degraded current
fallbacks when they lack filing/scope metadata, but they cannot silently satisfy
a strict normalized period gate. Yahoo indicators remain a current profile
snapshot, not an eight-period ratio history.

Factor/status/name routes use the same immutable plan but do not pass through
statement selection. Factor fallback remains sequential, status remains
BaoStock-authoritative, and name/ST capabilities remain split.

### Endpoint-aware caching and Provider Request Coordinator behavior

Add a run/checkpoint-scoped provider subrequest cache owned by the dispatcher or
the corresponding market acquisition owner. Its canonical key includes provider,
upstream/account scope, endpoint or ratio family, canonical symbol, requested
date/period range, requested field set, as-of date, qualification profile,
normalizer version, and Data Usage Mode.

- Identical subrequests share one in-flight or terminal result.
- One bulk Tushare or AKShare statement response can supply both annual and
  reporting-period projections without a second physical call.
- BaoStock family/year/quarter cache entries are independent.
- Terminal cache entries, artifacts, and outcomes are checkpoint-safe.
- Retained cross-run artifacts are evidence, not an implicit fresh response
  cache. A new run does not reuse one as current without an explicit future
  freshness policy.

Extend coordinator persistence additively with endpoint/capacity scope for
operator pacing and cooldown records while retaining one service-level lease and
one in-flight operation for the Tushare account. Global cooldown applies to all
endpoints; a provider-reported endpoint throttle applies to that endpoint scope.
Every attempt event records both Upstream Service Identity and safe endpoint
scope. Existing rows without scope read as `all`.

No adapter retry loop is permitted. Retry budgets, backoff, valid Retry-After,
cooldowns, circuits, priorities, and attempt counts remain coordinator-owned.
Fallback stays sequential and never fans out or probes until throttled.

### Work area F — Evidence manifests, completeness reporting, and degradation

Version the financial dispatch ledger, audit projection, and tool-message audit
envelope to reference:

- the immutable Capability Routing Plan identity;
- all provider artifact identities produced by the logical request;
- every Source Acquisition Outcome and physical-attempt event;
- all normalized period candidates and typed rejections;
- selected provider/period bindings;
- statement/ratio completeness assessments and since-listing exception;
- current-only, strict-PIT-eligible, degraded, insufficient, or conflicted
  disposition;
- the final rendered artifact identity.

Every successfully decoded provider response is retained operationally even if
normalization or completeness rejects all of its periods. Transport failures
with no usable payload retain a typed outcome and attempt evidence. Accepted
provider artifacts and the complete acquisition manifest are retained even if
no final model claim cites them.

Evidence projection remains explicit:

- Acquisition Outcomes and Source Artifacts may enter Evidence State.
- Unselected, rejected, supplemental, or current-only artifacts do not become
  Source Facts merely because they exist.
- Only selected normalized periods whose evidence contract and as-of rules pass
  may be projected into canonical Source Facts.
- Strict no-lookahead Source Facts require `f_ann_date` no later than the run
  as-of date and an eligible provider revision observed by that cutoff.
- Conflicted critical fields produce Conflicted Evidence for affected facts.
- Missing hard-gated required statements produce Insufficient Evidence.
- Missing optional breadth or current-only lineage produces Degraded Evidence
  only when the remaining evidence is still Decision-Ready for the proposed
  thesis.
- The Decision Gate and Decision Assertion rules remain unchanged.

Reports and audits add deterministic coverage tables by capability, statement,
period, provider, company type, metadata, selected/rejected disposition, and
typed reason. They report physical attempt counts from the coordinator, not
logical provider counts. They never include provider payload bodies, tokens, or
raw exception text.

### Work area G — Joint deterministic acceptance and parent regression

Combine work areas only after their focused tests pass. The joint seam is a
deterministic CLI/compiled-graph run with authoritative Instrument Identity,
fake coordinated provider transports, immutable runtime artifacts, checkpoint
round-trip, audit/report rendering, Evidence Admission, and terminal outcome.

Joint acceptance must run the new capability-routing criteria and the parent
market-data safety AC1-AC13 without waiver. Any parent failure blocks rollout
even when the new financial coverage tests pass.

### Dependencies and delivery order

| Work area | Depends on | May proceed concurrently with |
| --- | --- | --- |
| A. Contracts | Existing evidence/acquisition contracts and qualification decisions | None; lands first |
| B. Tushare adapters | A; safe config plan; coordinator endpoint scope | C and D after A |
| C. AKShare-Sina adapters | A; existing AKShare coordinator integration | B and D after A |
| D. BaoStock adapters | A; existing BaoStock history bundle and coordinator | B and C after A |
| E. Routing/selection | A and stable adapter contracts from B-D | F manifest scaffolding |
| F. Evidence/reporting | A; artifact store; E selection contract | E after shared schema is fixed |
| G. Joint acceptance | A-F complete | None; final gate |

### Migrations and compatibility

- Add coordinator endpoint/capacity-scope persistence through an atomic additive
  schema migration. Existing service, cooldown, lease, attempt, and audit rows
  remain readable; absent endpoint scope means `all`.
- Add versioned financial manifest, routing-plan projection, period-selection,
  and completeness fields. Existing v1 financial dispatch ledgers, reports,
  checkpoints, and audit projections remain readable and are never rewritten.
- A legacy checkpoint may resume only under the legacy routing plan and matching
  legacy provider-chain identity. Enabling `qualified_v1` against a checkpoint
  lacking its plan/manifest signature fails typed and starts a new run; it does
  not upgrade the checkpoint in place.
- Content-addressed runtime artifact storage remains the payload boundary. New
  manifest references reuse existing immutable artifact mechanics; no provider
  payload is embedded in the specification, source tree, checkpoint, report, or
  audit.
- Configuration additions have safe legacy defaults. No existing provider
  string is rewritten automatically.
- Existing market-history databases, snapshots, pins, payloads, reports,
  checkpoints, and provider records are preserved byte-for-byte unless a normal
  new run appends a versioned record.
- No dependency upgrade, including AKShare, is part of migration.

### Rollout and rollback

1. Land contracts, migrations, adapters, and tests with routing mode `legacy`.
2. Run deterministic fixture and parent AC1-AC13 validation; do not repeat live
   provider qualification.
3. Offer an explicit local `qualified_v1` shadow mode that records candidate
   manifests and completeness but leaves legacy selected financial output
   authoritative. Shadow runs are explicit validation workloads, not additional
   foreground requests in ordinary analysis.
4. Enable `qualified_v1` selection only for operators with coherent Tushare
   enablement/token/profile/pacing configuration. Capability routes activate
   independently according to the enabled set.
5. Keep an immediate configuration rollback to `legacy`. Rollback stops new
   qualified-route selection but retains all already written artifacts,
   manifests, reports, and audit records.
6. Global Tushare-first routing, Tushare authoritative suspension history, and
   strict Tushare Provider History Bundle status are not rollout states.

## Testing Decisions

### Test philosophy and seams

- Test observable behavior at the highest existing seam: financial dispatcher
  results and ledger, compiled financial tool node, provider coordinator with a
  fake transport, Authoritative Market Snapshot acquisition, artifact/audit
  projection, checkpoint round-trip, Evidence Admission, Decision Gate, and CLI
  terminal output.
- Use deterministic provider frames derived from the qualified schemas, fixed
  clocks, fake transports, and fake model clients. No test requires a live
  provider, token, paid account, or raw qualification payload.
- Assert selected periods, manifests, typed outcomes, artifact identities,
  physical call cardinality, evidence dispositions, and terminal contracts.
  Do not assert private helper order except documented coordinator and
  checkpoint boundaries.
- Reuse current financial dispatcher, financial graph, reporting, checkpoint,
  evidence artifact, decision audit, market snapshot, market-history,
  coordinator, BaoStock, AKShare, and Tushare PIT test patterns.

### Deterministic fixture matrix

- Shanghai non-bank with complete five-annual/eight-reporting Tushare coverage.
- Shenzhen mature non-bank with one Tushare period rejected and salvaged from
  AKShare-Sina.
- Bank with `comp_type=2`, bank-only critical fields, industrial fields absent,
  and sparse BaoStock operation/cash-flow ratios.
- Recent listing with authoritative listing date, fewer than five eligible
  annual periods, complete since-listing periods, and pre-listing rows that must
  not satisfy the exception.
- Provider responses with duplicate report periods, `update_flag` changes,
  conflicting critical values, missing `f_ann_date`, incompatible scope,
  non-CNY currency, ambiguous units, empty frames, malformed data, permission
  denial, authentication failure, timeout, rate limit, and Retry-After.
- Positive BaoStock traded/suspended status fixtures and an empty Tushare
  `suspend_d` supplemental fixture.
- Name rows containing ordinary, G-prefix, ST-looking, and actual dated status
  controls to prove names never define ST state.

### Acceptance criteria

| ID | Verifiable scenario | Required result |
| --- | --- | --- |
| CR1 | Resolve default/legacy configuration without Tushare financial enablement. | Existing market and financial provider chains, public tool contracts, checkpoint behavior, and output remain unchanged. |
| CR2 | Resolve `qualified_v1` with an enabled Tushare capability but missing token, wrong qualification profile, missing dependency, invalid pacing, or non-personal Data Usage Mode. | Typed configuration/usage outcome occurs before graph/model work; zero provider and model calls; no credential text is rendered. |
| CR3 | Build a qualified mainland plan. | Plan signature is deterministic, closed, checkpoint/audit-safe, secret-free, and commits to per-capability routes and policy versions. |
| CR4 | Acquire a daily mainland market snapshot under qualified routing. | Provider order remains AKShare -> BaoStock -> Yahoo; no Tushare call occurs; selected frame and Adjustment Basis follow existing semantics. |
| CR5 | Tushare returns complete balance, income, and cash-flow histories. | Each statement independently selects target periods; all provider artifacts and filing metadata are retained; no later statement provider is called for already complete target coverage. |
| CR6 | One Tushare statement endpoint fails while the other two succeed. | Successful statements/periods survive; only the failed statement continues to AKShare-Sina/Yahoo; physical attempts and typed failure are exact. |
| CR7 | One bulk statement response contains usable and unusable periods. | Usable independent periods are retained and selected; rejected periods carry typed reasons and continue fallback without discarding accepted periods. |
| CR8 | A bank payload omits industrial fields but fills its declared bank core. | Bank periods pass against the bank core; `comp_type=2` and classifier provenance are preserved; no dynamic active-field core is used. |
| CR9 | A company-type response is unknown, ambiguous, or contradicts another selected period. | Affected candidate is rejected or conflicted with typed reason; artifact remains retained; no silent cross-type merge occurs. |
| CR10 | Established issuer has at least five annual and eight reporting periods. | Exactly the latest eligible target sets are assessed; every selected period has 100% critical and at least 90% declared core coverage. |
| CR11 | Recent listing lacks five post-listing annual periods but has every expected period since listing. | Typed since-listing exception passes using authoritative listing provenance; pre-listing rows do not count; missing post-listing period fails the exception. |
| CR12 | A selected candidate has incompatible unit, currency, scope, or period identity. | It is not merged; typed rejection is recorded; fallback continues; unresolved hard-gate coverage becomes insufficient. |
| CR13 | Overlapping later-provider period matches or conflicts with a selected critical field. | Equivalent overlap is retained unselected; critical conflict marks only that period conflicted and blocks its Source Facts without erasing unrelated periods. |
| CR14 | Tushare statement metadata contains `ann_date`, `f_ann_date`, `report_type`, `comp_type`, and `update_flag`. | Every field survives normalization, manifest, checkpoint, audit, and report; `update_flag` alone is not labeled a stable restatement identity. |
| CR15 | AKShare-Sina supplies usable periods while another AKShare statement/subrequest fails. | Usable artifacts/periods survive; no internal Yahoo call occurs; dispatcher-level fallback is visible and sequential. |
| CR16 | Tushare indicators pass current completeness but lack `f_ann_date`. | Current-analysis indicator periods may be selected as current-only/degraded; strict no-lookahead Source Facts and replay are barred unless a valid filing binding exists. |
| CR17 | Higher indicator providers leave selected families/periods missing. | Only missing qualified BaoStock ratio families are called; usable families survive sparse/failed families; Yahoo is last and current-snapshot degraded only. |
| CR18 | A BaoStock ratio-family artifact is offered to a statement request. | Typed `capability_mismatch` prevents acceptance and statement fallback continues. |
| CR19 | Factor route runs with all providers available. | BaoStock is selected first; no later factor provider is called; strict bundle identity remains BaoStock and unchanged. |
| CR20 | BaoStock factor capability fails, Tushare succeeds, then a strict replay is requested. | Tushare factor can support the qualified factor/current path but cannot create a complete strict bundle; strict replay follows existing BaoStock/fail-closed rules. |
| CR21 | Only Yahoo-derived factor is available. | Result is labeled derived/degraded with lineage; it is not a native Adjustment Factor Revision or strict-bundle component. |
| CR22 | BaoStock proves current suspended/traded status while Tushare supplemental events are empty. | BaoStock alone determines Current Tradeability; empty Tushare data creates no traded fact; parent suspension Decision Gate behavior is unchanged. |
| CR23 | Name event contains `ST` or `*ST` text without authoritative dated ST status. | Name history is retained, but no ST/tradeability Source Fact is created; BaoStock dated status remains authoritative. |
| CR24 | Tushare returns permission, rate-limit, authentication, empty, malformed, and successful responses in separate cases. | Each maps to its exact typed outcome; shared endpoint/global cooldown behavior is deterministic; no retry or fallback fan-out occurs. |
| CR25 | Two logical requests require the same Tushare endpoint/symbol/range. | One coordinated physical request occurs; both logical results bind to the same artifact/cache identity; attempt accounting remains one. |
| CR26 | Annual and quarterly views use the same bulk statement response. | One physical endpoint call supplies both projections; period selection and tool-call correlation remain independent. |
| CR27 | Multiple BaoStock family/year/quarter gaps exist. | Physical attempts equal exactly the distinct missing cache keys; no satisfied family or period is re-requested. |
| CR28 | Model cites one selected fact and ignores other accepted datasets. | All accepted provider artifacts and the complete manifest remain in runtime artifacts, checkpoint, and audit independent of claims. |
| CR29 | Acquired artifacts include unselected, rejected, current-only, supplemental, and selected entries. | Only explicitly admitted selected/PIT-eligible projections may become Source Facts; coverage alone does not change Evidence Admission or Decision Gate. |
| CR30 | No provider satisfies a required statement hard gate. | Sequential fallback exhausts; rejected artifacts/outcomes remain operationally visible; result is Insufficient Evidence or typed unavailable, never fabricated or silently merged. |
| CR31 | Optional indicator breadth is missing while all material decision evidence remains valid. | Evidence may be Degraded with explicit coverage; Decision Assertion requirements remain unchanged. |
| CR32 | Checkpoint round-trip occurs with a qualified-v1 plan and partial selected periods. | Plan, cache, attempts, artifacts, candidates, selections, completeness, and degradation restore exactly without new provider calls. |
| CR33 | Resume a v1 checkpoint under legacy and qualified-v1 modes. | Legacy-compatible resume remains readable; qualified-v1 mismatch fails typed and starts a new run; no checkpoint/report rewrite occurs. |
| CR34 | Render CLI, report, checkpoint, audit, and strict replay fixtures. | New safe manifest/coverage fields are deterministic; legacy fixtures remain readable; payloads/tokens/raw provider errors never appear. |
| CR35 | Run a clean-tree default configuration scenario. | No new Tushare route activates; generated artifacts follow existing ignore/runtime rules; existing reports/snapshots/provider records remain unchanged. |
| CR36 | Disable qualified routing after artifacts have been written. | New runs use legacy routes immediately; prior artifacts/manifests remain readable and are not deleted or rewritten. |
| CR37 | Execute parent AC1-AC13. | Every parent criterion passes unchanged and without waiver. |
| CR38 | Review dependency and diff scope. | AKShare version is unchanged; no global Tushare-first route, authoritative Tushare suspension claim, Strategy Rule change, provider fan-out, or unrelated refactor exists. |

### Verification commands

Run focused contracts and financial routing first:

```powershell
pytest -q tests/test_financial_tool_dispatcher.py tests/test_financial_graph_dispatch.py tests/test_akshare_data.py tests/test_baostock_data.py tests/test_reporting.py tests/test_checkpoint_resume.py
```

Run provider coordination, market, status, evidence, audit, and replay checks:

```powershell
pytest -q tests/test_provider_request_coordinator.py tests/test_mainland_physical_attempt_coordination.py tests/test_market_snapshot.py tests/test_market_history_store.py tests/test_market_history_reconstruction.py tests/test_market_history_shadow.py tests/test_current_tradeability.py tests/test_evidence_artifacts.py tests/test_evidence_gates.py tests/test_admitted_evidence_binding.py tests/test_decision_audit.py tests/test_recorded_evidence_replay.py tests/test_tushare_pit_provider.py
```

Run deterministic non-live integration/CLI regressions and parent AC1-AC13
commands recorded by the parent acceptance effort. Then run repository-wide
verification:

```powershell
pytest -q -m "not integration"
pytest -q
ruff check .
git diff --check
git status --short
```

No acceptance command requires live Tushare, a token, a paid model, or repeated
provider qualification.

## Out of Scope

- Repeating provider qualification or performing live provider/account probes.
- Modifying production code as part of producing this specification.
- Committing raw provider payloads, credentials, tokens, token hashes, or
  unsanitized provider errors.
- Making Tushare globally first or adding Tushare to the daily market-snapshot
  chain.
- Treating Tushare `suspend_d` as authoritative suspension, trading-status, or
  Current Tradeability evidence.
- Claiming Tushare as a complete Strict History Provider or combining its factor
  with another provider's raw/status bundle for strict reconstruction.
- Inferring ST state from a company name or name-change event.
- Treating BaoStock ratios as full statements.
- Adding Tushare VIP cross-sectional interfaces.
- Upgrading AKShare or any unrelated dependency.
- Changing Strategy Rules, prompts, ratings, position sizing, predictive logic,
  Supported Crypto Universe, Data Usage Mode, or model retry behavior.
- Rewriting or deleting legacy reports, checkpoints, snapshots, pins, provider
  records, market-history databases, or immutable artifacts.
- Adding cross-provider market-row blending, provider fan-out, speculative
  prewarming, or foreground calls solely for future replay.
- General storage, CLI, evidence, or provider-framework refactoring beyond the
  smallest versioned contracts required by work areas A-G.

## Further Notes

- Authoritative qualification evidence is the original qualification report,
  the Tushare 2,000-point requalification report, its `/to-spec` amendment, the
  original specification amendment, and the generated Tushare summary named in
  the request. The implementation must not reinterpret documentation-only
  provider claims as qualification.
- This specification conforms to the accepted ADRs for single-provider market
  snapshots, fail-closed evidence, typed acquisition outcomes, point-in-time
  history, explicit suspension observations, factor derivation, centralized
  request coordination, bundle-atomic strict fallback, and mainland
  compatibility.
- Per-period financial selection is intentionally distinct from market-history
  row blending: each selected financial period retains its own provider artifact
  and Period Identity, while exact market facts still come from one
  Authoritative Market Snapshot.
- The implementation seam, configuration contract, failure taxonomy, caching
  scope, migration behavior, rollout, and all work-area dependencies are
  resolved. No design decision remains open; `ready-for-agent` is appropriate.

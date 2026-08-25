# 02I — Finalize fresh temporal eligibility after acquisition

**Parent specification:** [Mainland qualified routing production composition factory](../spec.md)

**What to build:** Resolve the Ticket 07 fresh-acquisition time-order defect with
one forward-only Eligibility Acquisition Window. The single-flight leader pins
the calendar and samples `eligibility_started_at` before retained/provider work,
then samples one `eligibility_evaluated_at` after the required company-type and
BaoStock lifecycle proofs are selected but before context binding. Only the
terminal evaluation emits authoritative temporal decisions.

**Blocked by:** 02G and 02H historical contracts, which remain readable but are
superseded for new writes by this amendment.

**Status:** ready-for-agent

## Decision basis

Run `601328.SS / 20260825_083711` retained truthful Tushare/Sina retrievals
after the pre-acquisition v1 evaluation instant. The v1 evaluator therefore
rejected every fresh proof as `retrieval_after_eligibility_evaluation`, produced
`shadow_dispatch_failed`, and never reached BaoStock lifecycle acquisition. A
deterministic public-seam reproduction advances the provider clock by one
second and produces the same result with no provider/model I/O.

The exact primary-source analysis and rejected options are recorded in
`../temporal-fresh-acquisition-policy-research-20260825.md`; ADR-0038 records
the accepted boundary decision.

## Observable behavior

- One leader samples `eligibility_started_at` before retained-state inspection
  or physical work and pins one exact calendar revision. Followers sample no
  clock, open no calendar, and perform no provider work.
- Provider adapters retain actual immutable `observed_at` and `retrieved_at`.
  No timestamp is clamped, backdated, inferred from request/session time, or
  replaced with a local date.
- Provider routing stages only structurally/provenance-qualified proof in the
  existing sequential plan order. Staging is not Evidence Admission and emits
  no authoritative temporal decision.
- After the required company-type and BaoStock lifecycle proofs are present,
  the leader samples one `eligibility_evaluated_at` and seals the transaction.
  No request factory or dispatcher is exposed before the seal succeeds.
- Fresh evidence requires
  `eligibility_started_at <= observed_at <= retrieved_at <= eligibility_evaluated_at`.
  Retained evidence requires its existing immutable ordering and
  `retrieved_at <= eligibility_evaluated_at`.
- Current analysis derives `latest_completed_session` from the same pinned
  calendar revision at terminal evaluation. If acquisition crosses the next
  open session's close, the requested session fails closed without a grace
  duration, provider fallback, or automatic date substitution.
- Strict PIT keeps its existing `strict_cutoff`, first-publication, direct
  observation/retrieval, and exact-revision availability predicates. The
  acquisition window never proves historical availability.
- Exact replay validates the persisted start/end/calendar/proof/decision inputs
  with zero current clock, calendar, evaluator, provider, coordinator, model,
  or attempt activity.

## Acceptance criteria

- [ ] A fresh built-in `601328.SS` fixture with nonzero Tushare and BaoStock
  latency binds one complete context and produces no
  `financial_dispatch_shadow_failure`.
- [ ] Fresh selected proofs retain their exact original timestamps and satisfy
  the closed start/observation/retrieval/evaluation ordering.
- [ ] Clock regression, future-dated retrieval, missing/mismatched transaction
  identity, or different calendar revision rejects typed and exposes no partial
  context/runtime.
- [ ] Starting before but finalizing at or after the next session close rejects
  the old requested session at the exact calendar boundary.
- [ ] Provider proof failures continue sequential fallback; a terminal window/
  frontier failure stops the context rather than calling a lower provider to
  repair run timing.
- [ ] Thirty-two followers share one leader, one start sample, one terminal
  evaluation sample, one pinned calendar revision, one selected proof set, one
  terminal result, and exact provider-attempt accounting.
- [ ] The original next-day `00:48:07 Asia/Shanghai`, weekend, holiday, retained
  future-data, strict-cutoff, and exact-revision matrices retain their intended
  results.
- [ ] Plan 1.3/context v2/v1 decisions and failed Ticket 07 namespaces remain
  byte-identical and replay only under their recorded readers.
- [ ] Legacy output remains authoritative, qualified output remains
  `shadow_non_authoritative`, and authoritative `qualified_v1` remains disabled.

## Exact forward versions

New writes use Capability Routing Plan `1.4` and pin:

- `provider-neutral-temporal-eligibility-v2`;
- `market-session-eligibility-binding-v2`;
- `financial-eligibility-acquisition-window-v1`;
- `financial-eligibility-temporal-transaction-binding-v1`;
- `financial-temporal-evidence-acquisition-binding-v1` with closed
  `fresh`/`retained` origin;
- `financial-metadata-temporal-eligibility-decision-v2`;
- `financial-eligibility-temporal-binding-v2`;
- Financial Eligibility Acquisition Context v3 and Request Binding v3;
- Financial Eligibility Metadata Binding v3;
- company-type acquisition-evidence binding v4, projection v3, routing request
  v3, and provider response v3;
- listing metadata/projection v3;
- acquisition request/result v3;
- statement/indicator routing requests v5/v8.

Any closed checkpoint/cache/ledger/manifest/audit/tool projection that embeds a
changed binding must advance its own version and remain enumerated by the plan
1.4 temporal contract inventory. Raw provider artifacts, candidates, physical
attempt events, and the `mainland-routing-plan:v1:<sha256>` namespace remain
unchanged.

## Migration and compatibility

- Historical plan 1.3, temporal policy/decision v1, calendar binding v1,
  context v2, cache 1.2, ledger 1.6, and their containing projections remain
  readable and are never rewritten or re-evaluated.
- A plan-1.4 run rejects or requires the existing explicit `START_NEW` path for
  any plan-1.3/context-v2 checkpoint. It never repairs or resumes a failed
  Ticket 07 namespace.
- Replay uses the recorded contract selected by the stored plan/context. It does
  not consult today's calendar frontier or synthesize a v2 window for v1 state.

## Explicit non-goals

- Grace windows, local-date ordering, next-session-open cutoffs, future calendar
  deadlines, timestamp rewriting, or retrieval-time proof backfill.
- Provider-order/fallback changes, fan-out, hidden retries, additional calendar
  reads, new providers, or threshold/completeness changes.
- Evidence Admission, Decision Gate, BaoStock authority, daily market routing,
  legacy authority, or activation-policy changes.
- Live provider/model work, failed-checkpoint resume, historical repair, or
  enabling authoritative `qualified_v1`.

## Required TDD and verification

Use the fresh built-in `FinancialDispatchToolNode` seam and a disposable pinned
calendar/provider set. First add the one-second-latency regression and watch it
produce `shadow_dispatch_failed`; then implement the smallest vertical slice
that binds a successful context. Add the close-boundary rejection and exact
historical replay controls before running the complete 02G/02H/05C/06B,
QCF1-QCF18, PE1-PE15, AC1-AC13, non-integration/full suites, Ruff, diff,
independent Spec/Standards review, one commit, and the required ISO chain.

## Definition of done

- [ ] Every acceptance criterion is proven at the public fresh/context/replay
  seams with zero live I/O.
- [ ] Forward identities are exact and historical readers/bytes are unchanged.
- [ ] Independent Spec and Standards reviews have no unresolved Critical or
  Important findings.
- [ ] One reviewed commit completes the source-fix cycle and is ready for the
  mandatory ISO-02 -> replacement ISO-03 -> additive baseline -> replacement
  ISO-04/Ticket 06 chain.

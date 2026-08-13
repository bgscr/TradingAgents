# CAL-04 — Deterministic bootstrap acceptance and two-stage review

**Parent specification:** [Mainland qualified routing production composition factory](../spec.md)

**What to build:** Prove the complete Design D calendar-bootstrap tracer bullet
against the real acquisition, coordinator, Market History Store, exact-ID
reader, readiness, programmatic service, and CLI seams using fake transports,
fixed clocks, and disposable stores. Run the full deterministic matrix and
compatibility checks, then complete Spec Compliance review before Code Quality
review. This ticket authorizes no live provider or model work.

**Blocked by:** CAL-03 — Explicit programmatic and CLI bootstrap.

**Status:** ready-for-agent

## Observable vertical-slice behavior

- From an empty disposable store, one explicitly authorized bounded fake
  acquisition performs one login/query/logout workflow, publishes one complete
  immutable revision, exact-reopens it, proves readiness, and returns `created`
  without allocating any analysis/model/checkpoint identity.
- Reuse, forced refresh, changed content, concurrency, cooldown, failure,
  tamper, and historical compatibility are observable through the public seams,
  exact durable rows/identities, safe receipts, and attempt records.
- Every read/replay path is zero-provider-I/O, and all live/provider/model test
  switches remain disabled.

## Acceptance criteria: complete deterministic matrix

- [ ] **Empty store bootstrap:** one authorized bounded fake acquisition records
  exactly login/query/logout, publishes one complete revision/ingestion audit,
  exact-reopens it, returns `created`, and creates no analysis/model/checkpoint
  state.
- [ ] **Exact-ID reopen:** the exact reader returns precisely the requested
  immutable revision and reproduces authority, digest, range, membership,
  timestamps, counts, and identity with zero provider I/O.
- [ ] **Completed frontier:** fixed `evaluated_at` plus persisted membership and
  contract-derived times yield the exact `latest_completed_session` and the
  requested/latest-session close.
- [ ] **Forward boundary:** the same exact revision yields the next open session
  and its close. Missing either fails `missing_forward_horizon`.
- [ ] **Ordinary identical rerun:** returns `reused`, the same semantic revision,
  and zero provider clients, permits, logical requests, transports, attempts, or
  new ingestion writes.
- [ ] **Forced identical refresh:** performs one new login/query/logout workflow,
  retains a later immutable ingestion audit, returns
  `refreshed_same_revision`, and reuses the semantic revision ID.
- [ ] **Changed calendar:** changed membership or session-time semantics creates
  one new immutable revision; the old revision and session rows remain exactly
  readable and unchanged.
- [ ] **Partial publication failures:** failure after every acquisition and
  publication phase leaves zero partial referencing revision/session/completed-
  ingestion rows and no partial receipt.
- [ ] **Incomplete range:** missing, duplicate, malformed, out-of-range, or
  contradictory civil-date membership fails `incomplete_range` without
  heuristic repair.
- [ ] **Stale coverage:** a complete stored range that cannot establish the
  evaluated completed/forward horizon fails `stale_coverage`.
- [ ] **Concurrent identical bootstrap:** all callers receive the same semantic
  revision/receipt content from one leader, one login/query/logout workflow, one
  publication, and one complete row set.
- [ ] **Active cooldown:** returns the existing typed cooldown outcome with zero
  transports/attempt events; a separate `10001005` control opens the shared
  global cooldown.
- [ ] **Reads/replay:** current read, exact-ID read, readiness, receipt
  verification, and persisted replay create zero provider clients, permits,
  logical requests, attempts, ingestion writes, or model work.
- [ ] **Safe metadata projection:** receipts, JSON, exceptions, logs, events, and
  IDs contain no credentials/state, raw provider errors/payloads, URLs/queries,
  absolute local paths, config dumps, or unbounded text.
- [ ] **Zero-calendar compatibility:** a historical database with no calendar
  revisions remains schema-valid and usable before bootstrap.
- [ ] **Historical immutability:** representative historical calendars,
  snapshots, pins, checkpoints, reports, artifacts, attempts, IDs, and all
  failed Ticket 07 evidence are byte-for-byte unchanged.
- [ ] **Identity/content tamper:** altered authority, digest, semantic contract,
  membership, times, or counts fails closed as
  `revision_identity_mismatch`; existing-row presence cannot mask tamper.
- [ ] **Authorization/range preflight:** missing authorization and bad/absent/
  unbounded ranges fail before client, permit, transport, attempt, publication,
  analysis allocation, or model construction.

## Explicit non-goals

- A real BaoStock call, production calendar publication, paid model invocation,
  Ticket 07 execution, Ticket 07 run-ID allocation, or analysis/checkpoint
  construction.
- Treating CAL-04 completion as authorization for the separately authorized
  production calendar bootstrap or for the separately authorized Ticket 07
  rollout.
- Enabling/recommending `qualified_v1`, changing daily/financial provider order,
  eligibility, Evidence Admission, Decision Gate, Current Tradeability authority,
  or shadow disposition.
- Copying/importing prototype implementation/tests; adding another calendar
  authority, writer, store, revision namespace, reader, cache, or router.
- Waiving, renumbering, weakening, or replacing QCF1–QCF18, PE1–PE15,
  AC1–AC13, or the completed 06B temporal matrix.

## Deterministic regressions

- Run calendar acquisition, store publication/reconstruction, exact reader,
  readiness/frontier, coordinator attempt/cooldown/single-flight,
  payload-mutation concurrency, API/CLI, and read-only preflight focused tests.
- Run relevant temporal eligibility and exact replay tests, including session-
  close boundaries, next-session forward horizon, weekends/holidays, and no
  restore-time current-calendar lookup.
- Run the complete QCF compatibility slice, preserving plan 1.3/context v2,
  indicator metadata compatibility, routes/fallback, attempts, checkpoint,
  `shadow_non_authoritative`, and disabled-authority assertions.
- Run the PE1–PE15 and AC1–AC13 compatibility slices exactly as specified by
  their authoritative tickets, without fixture/expectation changes or waiver.
- Run the full supported deterministic repository checks required below, Ruff,
  whitespace/diff checks, historical hash/inventory checks, and secret/raw-error
  canaries from one reviewed tree.

## Physical-attempt and accounting requirements

- Each acquired leader workflow is exactly one logical request and three ordered
  transports/events: `baostock_login`, `query_trade_dates`,
  `baostock_logout`. One physical transport equals one attempt event.
- Ordinary reuse, read/replay, readiness, exact verification, normalization,
  semantic revision reuse, publication, follower handoff, and model guards add
  zero attempt events.
- Concurrent identical bootstrap has one workflow total. Active cooldown has
  zero transports/events. Failure-path totals follow CAL-01 exactly and retain
  primary-error precedence.
- No hidden retry, adapter retry, fan-out, Provider Subrequest Cache misuse, or
  fabricated Instrument Identity is tolerated.

## Historical compatibility

- Hash/inventory representative historical database rows and filesystem
  material before and after acceptance. Zero-calendar state remains valid and
  all old calendar/snapshot/pin/checkpoint/report/artifact/attempt/ID contracts
  retain their recorded-version behavior.
- All previous failed Ticket 07 evidence remains immutable. CAL acceptance does
  not resume, repair, relabel, reinterpret, or reuse a prior run.
- ADR-0031, ADR-0036, temporal eligibility, plan 1.3/context v2, indicator
  metadata compatibility, provider routing/fallback, Evidence Admission,
  Decision Gate, BaoStock Current Tradeability authority, daily AKShare →
  BaoStock → Yahoo, `shadow_non_authoritative`, PE1–PE15, and AC1–AC13 remain
  unchanged. `qualified_v1` remains disabled.

## Security requirements

- Test guards prove no live SDK/provider/model is initialized and no environment
  credential value is read into a public/persisted surface.
- Authorization remains ephemeral. Safe metadata and failures exclude secret
  state, raw payload/error text, URLs/queries, config dumps, absolute local
  paths, analysis/checkpoint IDs, and unbounded diagnostics.
- Tamper and contradiction fail closed without repair, provider I/O, or unsafe
  diagnostic expansion.

## Verification commands

Run targeted calendar/store/coordinator and temporal compatibility first:

```powershell
rtk pytest -q tests/test_baostock_session_calendar_acquisition.py tests/test_market_session_calendar_publication.py tests/test_market_session_calendar_exact_reader.py tests/test_market_session_calendar_readiness.py tests/test_market_session_calendar_bootstrap.py
rtk pytest -q tests/test_provider_request_coordinator.py tests/test_mainland_physical_attempt_coordination.py tests/test_market_history_payload_gc_concurrency.py -k "calendar or baostock or cooldown or single_flight or publication"
rtk pytest -q tests/test_financial_metadata_temporal_eligibility.py tests/test_financial_temporal_proof_projection.py tests/test_financial_temporal_checkpoint_compatibility.py
```

Run the unchanged QCF, PE, and AC compatibility slices and full deterministic
checks:

```powershell
rtk pytest -q tests/test_qualified_routing_composition_acceptance.py tests/test_qualified_routing_rollout.py tests/test_corrected_shadow_joint_acceptance.py
rtk pytest -q tests/test_financial_period_targets.py tests/test_financial_period_availability.py tests/test_financial_period_contracts.py
rtk pytest -q tests/test_market_snapshot.py tests/test_market_history_store.py tests/test_market_history_reconstruction.py tests/test_market_history_shadow.py tests/test_current_tradeability.py
rtk pytest -q -m "not integration"
rtk pytest -q
rtk ruff check .
git diff --check
git status --short
```

All live-provider and paid-model flags must be explicitly disabled. If any
command would contact a live system, stop and correct the deterministic seam
instead of running it.

## Two-stage review

1. **Spec Compliance:** verify every Design D authority/contract, CAL dependency,
   public behavior, typed failure, semantic identity, readiness rule, attempt
   count, compatibility invariant, security boundary, and non-goal. Classify
   findings as Critical, Important, or Minor and resolve every Critical and
   Important finding before Stage 2.
2. **Code Quality:** review failure/cleanup precedence, transaction durability,
   concurrency/single-flight, safe typing/projection, test determinism,
   maintainability, reuse of existing owners, and absence of unrelated changes.
   Classify findings identically and resolve every Critical and Important
   finding.

## Definition of done

- [ ] Every row of the complete deterministic matrix passes through public seams
  with exact identities, readiness results, durable state, and attempt counts.
- [ ] Focused calendar/coordinator/store tests, temporal eligibility, QCF, PE,
  AC, non-integration, full deterministic suite, Ruff, and `git diff --check`
  pass from the same reviewed tree without live provider/model work.
- [ ] Spec Compliance passes before Code Quality; both reviews record Critical,
  Important, and Minor counts, and all Critical/Important findings are resolved.
- [ ] Historical/security invariants pass, `qualified_v1` remains disabled, and
  no production bootstrap or Ticket 07 execution/run-ID allocation occurs.
- [ ] The downstream gate is recorded exactly and is not executed by CAL-04:

  `CAL-04 → separately authorized production calendar bootstrap → exact-ID readiness verification → complete Ticket 06 rerun on the exact reviewed post-CAL production tree → fresh Ticket 07 authorization → NEW 601328.SS run → NEW 600895.SS only after bank PASS`.


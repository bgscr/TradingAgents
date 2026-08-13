# CAL-03 — Explicit programmatic and CLI bootstrap

**Parent specification:** [Mainland qualified routing production composition factory](../spec.md)

**What to build:** Expose one production application service,
`bootstrap_mainland_session_calendar(...)`, and one equivalent maintenance CLI
command that validates/reuses persisted readiness first, performs CAL-01
acquisition only with explicit authorization when required or forced, publishes
through CAL-02's sole writer, reopens the exact returned revision ID, and emits a
safe deterministic receipt. Qualified-analysis preflight remains read-only.

**Blocked by:** CAL-02 — Publication/readiness hardening and exact revision
reader.

**Status:** ready-for-agent

## Observable vertical-slice behavior

- Programmatic callers submit the closed
  `market-session-calendar-bootstrap-request-v1` contract and receive the closed
  `market-session-calendar-bootstrap-receipt-v1` contract.
- The maintenance command is equivalent to:

  `tradingagents market-session-calendar-bootstrap --reference-market mainland-cn --provider baostock --start-date YYYY-MM-DD --end-date YYYY-MM-DD --evaluated-at <RFC3339> --authorize-provider-io`

  with optional `--force-refresh`.
- An ordinary identical request first verifies the persisted exact revision and
  readiness, then returns `reused` with zero provider I/O and no new ingestion
  audit.
- An explicitly forced identical refresh may acquire again, returns
  `refreshed_same_revision`, reuses the semantic revision, and retains a later
  immutable ingestion audit. Changed semantic content returns `created` with a
  new revision and leaves the old revision intact.
- Before returning, both API and CLI reopen the exact returned revision by ID and
  verify complete readiness; neither verifies by racing through a second current
  lookup.

## Acceptance criteria

- [ ] API and CLI use the same application service and the same request,
  acquisition, publication, exact-reopen, readiness, receipt, and error
  contracts. There is no duplicate bootstrap implementation.
- [ ] The request requires literal `baostock`, `mainland-cn`, `Asia/Shanghai`,
  explicit inclusive start/end dates, timezone-aware fixed `evaluated_at`, Data
  Usage Mode/safe coordinator scope, and ephemeral provider-I/O authorization
  whenever acquisition is required. `force_refresh` is explicit and optional.
- [ ] The request neither requires nor accepts a symbol, Instrument Identity,
  Ticket 07 requested-session parameter, analysis run ID, checkpoint namespace,
  Capability Routing Plan, model configuration, or `qualified_v1` state.
- [ ] Missing provider-I/O authorization fails
  `operator_authorization_required` before provider-client construction,
  coordinator permit, transport/attempt, publication, analysis allocation, or
  model work. Persisted-ready zero-I/O reuse does not require provider I/O.
- [ ] Invalid, absent, reversed, empty, or unbounded ranges and invalid RFC3339
  evaluation instants fail typed before transport. The accepted inclusive range
  appears exactly in the safe receipt.
- [ ] The service validates persisted readiness before acquisition. An ordinary
  identical ready bootstrap returns `reused` with the same semantic revision,
  zero provider I/O/permits/logical requests/attempts, and zero ingestion writes.
- [ ] `force_refresh` is the sole ordinary way to reacquire an already-ready
  identical request. Identical semantics reuse the revision and retain a later
  audit; changed membership/time semantics create one new immutable revision.
- [ ] The returned revision is reopened through
  `read_session_calendar_revision(calendar_revision_id)` and must reproduce
  exact authority, content, identity, range, session times, latest-completed
  frontier/close, and later open-session/close with zero read-time provider I/O.
- [ ] The CLI emits one deterministically ordered JSON receipt and an outcome-
  specific exit code. The receipt disposition is exactly `created`, `reused`,
  or `refreshed_same_revision`.
- [ ] The receipt carries only the safe fields settled in Design D: pinned
  authorities/contracts, requested range/evaluation instant, revision and
  ingestion identities, audit timestamps, digest/counts, completed and forward
  session boundaries, safe coordinator workflow/attempt identities and counts,
  and whether provider I/O occurred.
- [ ] The service allocates no analysis run/checkpoint identity, constructs no
  qualified composition/graph/model, performs no symbol work, and does not
  execute Ticket 07.
- [ ] Qualified analysis preflight remains a read-only exact revision consumer
  with no hidden acquisition, refresh, repair, publication, authorization flag,
  or fallback.

## Explicit non-goals

- Live production calendar bootstrap authorization or execution. CAL-03 builds
  and deterministically verifies the surface; the real bootstrap is a separate
  future operator authorization after CAL-04.
- Ticket 07 authorization/execution, run-ID allocation, model construction, or
  symbol-specific history acquisition.
- Enabling/recommending `qualified_v1`, adding a bootstrap option to ordinary
  analysis, or allowing analysis preflight to write/repair calendar state.
- A second acquisition path, publisher/store/revision namespace, current-reader
  race, provider router/fallback, Provider Subrequest Cache entry, or fabricated
  Instrument Identity.
- Copying/importing the calendar-bootstrap prototype implementation or tests.

## Deterministic regressions

- Exercise API/CLI parity for created, reused, refreshed-same-revision, changed-
  revision, every readiness failure, every acquisition/coordinator failure, and
  every publication/exact-reopen failure.
- Assert deterministic JSON key order, values, counts, dispositions, and exit
  codes across repeat runs and process restarts.
- Prove missing authorization and invalid ranges fail before provider/client/
  permit/attempt/storage/model/run allocation.
- Prove ordinary identical readiness reuse is zero-I/O and zero-write; forced
  identical refresh is one CAL-01 workflow and the same semantic revision with a
  later audit.
- Prove exact-ID reopen is used even when a concurrent publisher changes the
  current revision between publication and verification.
- Prove qualified analysis and Ticket 07 preflight remain read-only and cannot
  call the bootstrap service implicitly.

## Physical-attempt and accounting requirements

- Created or forced-refresh acquisition records the CAL-01 workflow exactly:
  one login, one query, one logout transport/event under one logical request.
- Ordinary identical reuse, exact-ID reopen, readiness, JSON rendering, and CLI
  help/error handling perform zero provider transports/attempts.
- Concurrent identical bootstraps join one workflow and one publication; no
  follower, retry wrapper, CLI layer, or receipt verification duplicates an
  attempt.
- Active cooldown remains zero transports/events, and code `10001005` retains
  the existing shared-cooldown semantics.

## Historical compatibility

- The command is additive. Existing CLI commands, programmatic analysis,
  zero-calendar databases, calendars, snapshots, pins, reports, checkpoints,
  attempts, IDs, and failed Ticket 07 evidence remain unchanged/readable.
- Temporal eligibility, plan 1.3/context v2, indicator metadata compatibility,
  provider routing/fallback, Evidence Admission, Decision Gate, BaoStock Current
  Tradeability authority, daily AKShare → BaoStock → Yahoo,
  `shadow_non_authoritative`, PE1–PE15, and AC1–AC13 remain authoritative.
- `qualified_v1` remains disabled before and after every bootstrap test.

## Security requirements

- The authorization value is ephemeral and never appears in receipt, logs,
  persisted rows, IDs, config projections, or reprs.
- JSON, typed failures, logs, events, and IDs exclude credentials/credential
  state, raw provider errors/payloads, URLs/queries, config dumps, machine-local
  absolute paths, analysis/checkpoint IDs, and unbounded diagnostic text.
- CLI validation never prints environment values or raw provider exceptions.

## Verification commands

```powershell
rtk pytest -q tests/test_market_session_calendar_bootstrap.py
rtk pytest -q tests/test_cli.py -k "market_session_calendar_bootstrap"
rtk pytest -q tests/test_qualified_routing_rollout.py -k "calendar and preflight and read_only"
rtk ruff check .
git diff --check
```

Use fake transports, disposable stores, fixed clocks, and fake/no-op model
guards. Do not run the real bootstrap command against a production store or
contact BaoStock during this ticket.

## Definition of done

- [ ] Programmatic and CLI paths share one implementation and produce exact safe
  request/receipt behavior for created, reused, and refreshed-same-revision.
- [ ] Provider authorization, range validation, persisted-ready zero-I/O reuse,
  force-refresh semantics, exact-ID reopen, structural readiness, deterministic
  JSON, and outcome exit codes pass at public seams.
- [ ] No analysis/checkpoint/model/symbol/Ticket 07 work, implicit provider I/O,
  second writer/cache/router, prototype copy, or `qualified_v1` activation is
  introduced.
- [ ] Physical-attempt, historical compatibility, and security invariants pass;
  Ruff and `git diff --check` pass; deterministic evidence is recorded.


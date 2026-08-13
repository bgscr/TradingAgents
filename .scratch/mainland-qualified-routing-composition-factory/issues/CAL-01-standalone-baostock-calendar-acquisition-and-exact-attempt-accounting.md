# CAL-01 — Standalone BaoStock calendar acquisition and exact attempt accounting

**Parent specification:** [Mainland qualified routing production composition factory](../spec.md)

**What to build:** Deliver one safe, standalone, Instrument-neutral acquisition
path that turns an explicitly authorized, bounded BaoStock
`query_trade_dates(start_date, end_date)` response into a typed canonical
mainland Market Session Calendar candidate. The observable workflow is one
lease-owned logical request with exactly one `baostock_login`, one
`query_trade_dates`, and one `baostock_logout` physical operation, each recorded
through the ADR-0031 Provider Request Coordinator lifecycle. This ticket stops
before calendar persistence.

**Blocked by:** None — can start immediately. The accepted calendar-bootstrap
Design D, ADR-0031, ADR-0036, and the existing BaoStock/coordinator contracts are
authoritative inputs, not implementation tickets.

**Status:** ready-for-agent

## Observable vertical-slice behavior

- An operator-authorized request names provider `baostock`, Reference Market
  `mainland-cn`, timezone `Asia/Shanghai`, dataset
  `mainland-session-calendar-v1`, normalizer
  `baostock-session-calendar-normalizer-v1`, and an explicit bounded inclusive
  civil-date range.
- The provider primitive calls only
  `query_trade_dates(start_date, end_date)` and derives open/closed membership
  only from BaoStock `is_trading_day`.
- The request and result contain no symbol, ticker, Instrument Identity,
  requested Ticket 07 session, analysis run ID, checkpoint identity, financial
  route, or model input.
- A successful leader returns a safe immutable typed acquisition containing one
  normalized membership record for every civil date in the requested range and
  safe workflow/attempt metadata. No calendar revision or session row is
  persisted by this ticket.
- Concurrent identical requests join one single-flight leader and observe the
  same terminal typed result. They do not create parallel logins, queries, or
  logouts.

## Acceptance criteria

- [ ] The request contract rejects absent, empty, reversed, unbounded, or
  out-of-policy ranges before provider-client construction, coordinator permit,
  or transport. The exact inclusive bounds participate in the logical request
  identity.
- [ ] Provider I/O is denied unless the caller supplies the explicit ephemeral
  provider-I/O authorization required by Design D. Missing authorization fails
  typed before any transport or physical-attempt event.
- [ ] The authority is exactly BaoStock / `query_trade_dates` /
  `mainland-cn` / `Asia/Shanghai` / `mainland-session-calendar-v1` /
  `baostock-session-calendar-normalizer-v1`.
- [ ] Membership is derived only from `is_trading_day`. Duplicate, missing,
  out-of-range, malformed, contradictory, or empty provider rows fail typed;
  weekdays, OHLCV, picker state, symbols, prices, names, request dates, local
  clock dates, and other providers never fill a gap.
- [ ] One successful workflow records exactly three ordered physical operations:
  `baostock_login`, `query_trade_dates`, and `baostock_logout`. One physical
  transport invocation produces one and only one attempt event.
- [ ] Query begins only after successful login. When login succeeds, logout is
  attempted and accounted after query success or query failure; a query/provider
  failure remains primary if logout cleanup also fails.
- [ ] Every operation uses the existing ADR-0031 coordinator attempt lifecycle,
  upstream service identity, Operator Safety Ceiling, and shared cooldown. There
  is no adapter-local or hidden retry and no parallel provider fan-out.
- [ ] BaoStock login-capacity code `10001005` opens the existing shared global
  cooldown. A request arriving during active cooldown returns the existing safe
  typed cooldown result with zero provider transports and zero attempt events.
- [ ] Identical concurrent acquisition requests single-flight under the same
  provider/upstream/account/capacity scope, endpoint/dataset, bounded range,
  semantic contracts, Reference Market, timezone, and Data Usage Mode. A
  materially different key does not collide.
- [ ] The result is closed, immutable, deterministic, and safe to serialize. Raw
  provider payloads/errors and credentials cannot cross the provider boundary.
- [ ] The Instrument-specific Provider Subrequest Cache is not used and no
  fabricated Instrument Identity is introduced for durable or in-flight reuse.

## Explicit non-goals

- Publishing a Market Session Calendar revision, writing session rows, adding an
  ingestion audit, or implementing readiness/exact-ID reads; those belong to
  CAL-02 and later.
- Adding another calendar provider, router, fallback order, database, cache,
  revision namespace, or process-global client/session.
- Calling the symbol-coupled BaoStock history-bundle workflow or inferring
  membership/session times from price data.
- Copying or importing the calendar-bootstrap prototype implementation or tests
  into production.
- Running a live provider, paid model, production calendar bootstrap, Ticket 07,
  or any analysis/model/checkpoint construction.
- Enabling or recommending `qualified_v1`. It remains disabled.

## Deterministic regressions

- Cover every valid `is_trading_day` encoding accepted by the pinned normalizer
  and every missing/duplicate/malformed/contradictory/out-of-range failure.
- Cover absent authorization and every invalid-range class before client,
  permit, transport, attempt, or persistence work.
- Cover login failure, `10001005`, query failure, logout failure, interruption,
  cleanup failure, and primary-error precedence with exact transport/event
  counts.
- Cover an already-active cooldown with zero transports/events, plus a separate
  control proving `10001005` opens shared cooldown.
- Cover at least 32 identical concurrent followers producing one
  login/query/logout leader workflow and one immutable terminal result.
- Prove a changed range, semantic contract, provider scope, or Data Usage Mode
  does not join the wrong single-flight key.

## Physical-attempt and accounting requirements

- A successful acquisition has one logical request, three physical transports,
  and three attempt events in the exact order login → query → logout.
- Failed login has one transport/event and no query or logout. Active cooldown
  has zero transports/events. Successful login followed by query failure has an
  accounted query and an accounted logout attempt.
- Cache hits, normalization, validation, typed result construction, follower
  handoff, and skipped operations create no physical-attempt events.
- Attempt identities/outcomes and counts are safe and deterministic; provider
  payload bodies and raw error text never become attempt metadata.

## Historical compatibility

- Existing zero-calendar Market History databases remain valid and untouched.
- No historical calendar, market snapshot, pin, report, checkpoint, provider
  artifact, attempt, ID, or failed Ticket 07 evidence is rewritten, migrated,
  deleted, relabeled, or reinterpreted.
- ADR-0036 and all temporal eligibility contracts remain unchanged. Plan 1.3,
  Financial Eligibility Acquisition Context v2, indicator metadata
  compatibility, Evidence Admission, Decision Gate, BaoStock Current
  Tradeability authority, daily AKShare → BaoStock → Yahoo routing,
  `shadow_non_authoritative`, PE1–PE15, and AC1–AC13 are preserved.

## Security requirements

- Credentials remain inside the existing environment/provider boundary and are
  absent from requests, results, identities, reprs, events, logs, exceptions,
  checkpoints, reports, and IDs.
- Safe failures expose only the closed typed reason and bounded safe metadata;
  they exclude raw BaoStock errors/payloads, URLs/queries, account secrets,
  machine-local absolute paths, and unbounded diagnostics.
- Authorization is explicit, ephemeral, non-persisted, and checked before any
  provider resource is opened.

## Verification commands

Run only deterministic fake-transport tests with all live-provider/model flags
disabled:

```powershell
rtk pytest -q tests/test_baostock_session_calendar_acquisition.py
rtk pytest -q tests/test_provider_request_coordinator.py tests/test_mainland_physical_attempt_coordination.py -k "baostock or cooldown or single_flight"
rtk ruff check .
git diff --check
```

Do not select integration/live tests and do not import or initialize the live
BaoStock SDK as part of verification.

## Definition of done

- [ ] The standalone bounded Instrument-neutral acquisition passes every
  authority, normalization, authorization, failure, interruption, and
  single-flight acceptance criterion through fake transports and fixed clocks.
- [ ] Exact logical/physical/attempt accounting is demonstrated for success and
  every terminal path, including `10001005` and active cooldown.
- [ ] No persistence, symbol/Instrument Identity, Provider Subrequest Cache,
  hidden retry, live provider/model, analysis runtime, or Ticket 07 work occurs.
- [ ] Historical/security invariants and `qualified_v1` disabled state are
  verified, Ruff and `git diff --check` pass, and the ticket records the exact
  deterministic command results.


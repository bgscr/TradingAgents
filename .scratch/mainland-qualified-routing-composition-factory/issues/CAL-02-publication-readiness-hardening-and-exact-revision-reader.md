# CAL-02 — Publication/readiness hardening and exact revision reader

**Parent specification:** [Mainland qualified routing production composition factory](../spec.md)

**What to build:** Publish a validated CAL-01 calendar candidate through the
existing `MarketHistoryStore.publish_session_calendar()` authority, then reopen
the exact returned immutable revision and prove that it is structurally ready.
The end-to-end slice owns deterministic semantic revision identity, atomic
revision/session publication, exact verification of already-known content, the
narrow `read_session_calendar_revision(calendar_revision_id)` reader, and the
closed readiness result. It does not add a second writer, store, table family,
or revision namespace.

**Blocked by:** CAL-01 — Standalone BaoStock calendar acquisition and exact
attempt accounting.

**Status:** ready-for-agent

## Observable vertical-slice behavior

- A complete BaoStock membership candidate is combined with
  `mainland-cn-regular-session-times-v1`: every open civil date has contract-
  derived `09:30:00` open and `15:00:00` close in `Asia/Shanghai`; every closed
  date has null open/close timestamps.
- The canonical payload uses `mainland-session-calendar-payload-v1`. Its
  readiness assessment uses `mainland-session-calendar-readiness-v1`.
- The existing publisher atomically makes one complete immutable semantic
  revision and all of its session rows visible, or makes none of them visible.
- Exact-ID reopen returns only the requested immutable revision, reconstructs
  sorted canonical content, recomputes digest/identity, and performs zero
  provider I/O.
- At one fixed `evaluated_at`, structural readiness yields the exact latest
  completed session and its close plus a later open session and its close.

## Acceptance criteria

- [ ] `MarketHistoryStore.publish_session_calendar()` remains the sole writer,
  using the existing `market-session-calendar=sha256:<digest>` namespace,
  payload-mutation mutex, calendar tables, ingestion model, and SQLite
  transaction boundary.
- [ ] Provider membership and contract-derived session times are labeled
  distinctly. No open/close time is represented as provider-returned.
- [ ] Semantic revision identity deterministically binds provider/upstream/
  endpoint/dataset authority, `mainland-cn`, `Asia/Shanghai`, observed-point-in-
  time provenance, normalizer/payload/time contracts, canonical membership, and
  normalized session timestamps.
- [ ] Retrieval, observation, ingestion, publication, and other audit timestamps
  do not affect semantic revision identity.
- [ ] Identical semantic content reuses the existing revision ID. A changed
  membership, timezone interpretation, regular-session time, or semantic
  contract produces a new immutable revision while retaining the old revision
  and rows.
- [ ] A forced semantically identical publication may retain a later immutable
  calendar-refresh ingestion audit, but it reuses the same semantic revision ID
  and does not mutate the previously published revision/session rows.
- [ ] Publication of an already-known revision recomputes and compares every
  authority field, digest, membership row, and session timestamp exactly.
  `INSERT OR IGNORE` or key presence alone is not accepted as content proof.
- [ ] Revision metadata, all session rows, and the completed calendar-refresh
  ingestion state publish atomically. Failure injected at any publication phase
  leaves no partial referencing revision, session rows, or completed ingestion
  state visible.
- [ ] `read_session_calendar_revision(calendar_revision_id)` is a narrow public
  exact reader, with the repository-equivalent coordinated read where existing
  lifecycle ownership requires it. It never substitutes the current revision.
- [ ] Exact-ID read reconstructs sorted content and verifies requested identity,
  authority, digest, counts, membership, open/close timestamps, and contract
  versions. Any mismatch fails `revision_identity_mismatch`.
- [ ] Readiness verifies complete requested inclusive range, pinned authority,
  valid session-time shape, derivable latest completed frontier, frontier close,
  at least one later open session and its close, structural freshness, and exact
  identity/content reopen.
- [ ] The closed typed readiness failures are exactly supported:
  `calendar_missing`, `authority_mismatch`, `incomplete_range`,
  `missing_session_times`, `unresolved_completed_frontier`,
  `missing_forward_horizon`, `stale_coverage`, and
  `revision_identity_mismatch`.
- [ ] Current read, exact-ID read, readiness, identity verification, and replay
  create zero provider clients, coordinator permits/logical requests, physical
  transports/attempt events, or ingestion writes.

## Explicit non-goals

- A second writer/store/schema/table family/revision namespace, current-reader
  replacement, calendar router, or picker/symbol history dependency.
- The programmatic/CLI bootstrap orchestration and operator receipt; those belong
  to CAL-03.
- Treating database/file age, local date, weekdays, elapsed grace, next-session
  open, price rows, or request intent as calendar membership/freshness authority.
- Modifying temporal eligibility, provider routing/fallback, Evidence Admission,
  Decision Gate, BaoStock Current Tradeability authority, or indicator metadata
  compatibility.
- Copying the calendar-bootstrap prototype implementation, running live
  providers/paid models, executing Ticket 07, or enabling `qualified_v1`.

## Deterministic regressions

- Prove canonical ordering/serialization invariance and semantic identity reuse
  across byte/row-order/audit-time differences.
- Prove membership or session-time changes create a new revision and leave the
  old exact-ID read byte/content stable.
- Fault-inject every store publication boundary and verify all-or-nothing
  revision, session-row, and completed-ingestion visibility.
- Corrupt or tamper each authority, digest, contract, membership, time, and count
  dimension and require `revision_identity_mismatch` on exact reopen.
- Exercise every closed readiness failure and the success boundary immediately
  before, at, and after a session close with a fixed clock.
- Prove complete range versus structural freshness are distinct:
  `incomplete_range` for a missing/duplicate/contradictory civil date and
  `stale_coverage` for complete stored bounds that do not reach the required
  evaluation/forward horizon.
- Prove existing databases containing zero calendar revisions open and operate
  without migration of historical data.

## Physical-attempt and accounting requirements

- CAL-02 publication, exact reads, readiness checks, identity checks, and replay
  are provider-free operations and therefore record zero provider transports and
  zero physical-attempt events.
- A CAL-01 acquisition audit may be referenced, but publication must not invent,
  duplicate, collapse, or rewrite its login/query/logout attempt events.
- Semantic revision reuse and new-revision publication are storage outcomes, not
  provider attempts. A later ingestion audit is not a physical-attempt event.

## Historical compatibility

- Existing calendar revisions, zero-calendar databases, snapshots, pins,
  ingestion records, attempts, reports, checkpoints, IDs, and failed Ticket 07
  evidence remain readable and immutable.
- No in-place migration, revision renumbering, reinterpretation, backfill, or
  deletion is permitted. New semantics are forward-only under the pinned
  normalizer/payload/time/readiness contracts.
- ADR-0031, ADR-0036, temporal eligibility, plan 1.3/context v2, financial-
  indicator metadata compatibility, provider routes/fallback, Evidence
  Admission, Decision Gate, BaoStock authority, daily AKShare → BaoStock →
  Yahoo, `shadow_non_authoritative`, PE1–PE15, and AC1–AC13 remain unchanged.

## Security requirements

- Exact readers and readiness failures expose only bounded safe metadata. They
  contain no credential state, raw provider payload/error, URL/query, config
  dump, machine-local absolute path, analysis/checkpoint identity, or unbounded
  diagnostic text.
- Stored identities and audit rows contain no ephemeral provider-I/O
  authorization or credentials.
- Tampered or mismatched content fails closed; it is never repaired or silently
  accepted during read/reopen.

## Verification commands

```powershell
rtk pytest -q tests/test_market_history_store.py tests/test_market_session_calendar_publication.py
rtk pytest -q tests/test_market_session_calendar_exact_reader.py tests/test_market_session_calendar_readiness.py
rtk pytest -q tests/test_market_history_payload_gc_concurrency.py -k "calendar or publication or rollback"
rtk ruff check .
git diff --check
```

All fixtures use disposable stores and fixed clocks. No verification command may
initialize a live provider or paid model.

## Definition of done

- [ ] The existing publisher atomically publishes or semantically reuses a
  complete immutable revision and never exposes partial revision/session state.
- [ ] Exact-ID reopen and every readiness success/failure are deterministic,
  typed, identity-checked, and zero-provider-I/O.
- [ ] Identical content reuses one revision; changed membership/time semantics
  creates a new revision while old content remains exact and immutable.
- [ ] All historical/security/accounting invariants pass, no second authority or
  prototype code is introduced, Ruff and `git diff --check` pass, and the exact
  deterministic evidence is recorded.

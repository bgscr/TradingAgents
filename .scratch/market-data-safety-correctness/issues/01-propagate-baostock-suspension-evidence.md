# 01 — Propagate authoritative BaoStock suspension evidence

Status: ready-for-agent

Blocked by: None — can start immediately

## Parent specification

[Market-data trading safety and point-in-time correctness](../spec.md). Its cross-cutting invariants, compatibility requirements, acceptance definitions, and Out of Scope section are binding.

## Problem being solved

A complete BaoStock Provider History Bundle can carry authoritative latest-session trading status, yet that status or its latest genuinely traded close can be weakened or lost as the selected candidate crosses live snapshot, History Store Degradation, Canonical Evidence Contract, and decision-audit boundaries. Downstream safety cannot enforce [ADR-0023](../../../docs/adr/0023-block-decisions-for-current-mainland-suspensions.md) unless the accepted provider evidence arrives intact.

## What to build

Deliver one observable BaoStock path in which the accepted complete provider candidate carries its explicit Suspension Observation or traded status, status provenance, and latest genuinely traded close through provider validation, candidate selection, Authoritative Market Snapshot construction, History Store Degradation, canonical evidence, and audit serialization.

- Derive Current Tradeability from the latest applicable mainland Market Session represented by authoritative status evidence.
- Preserve the official carried close and zero volume in the equity Observation Horizon while reporting `latest_traded_close` from the most recent explicitly traded session under the snapshot's Adjustment Basis.
- Keep `latest_traded_close` unavailable, with an explicit diagnostic, when no genuinely traded retained session exists.
- Treat blank or zero-volume rows without authoritative status as malformed or `unknown`, never as suspension evidence.
- Stop provider fallback after a selected complete BaoStock candidate; do not call another provider merely to replace a confirmed suspension or prepare replay.
- Fail closed on contradictions or status loss between the accepted provider bundle and the constructed snapshot.

## Explicit non-goals

- Do not implement the terminal suspended-instrument Decision Gate; Ticket 02 owns that behavior.
- Do not introduce `snapshot:v2` identity or change durable pin membership; Ticket 03 owns those changes.
- Do not infer suspension from price, volume, blanks, or model prose.
- Do not reorder the Current Analysis Provider Chain, blend providers, change Adjustment Basis rules, or change any Strategy Rule.
- Do not refactor unrelated provider, evidence, or reporting code.

## Blocking dependencies

None. This ticket blocks Ticket 02 and Ticket 03 because both require the authoritative status-bearing snapshot contract established here.

## External behavior and audit contract

- The selected BaoStock snapshot and canonical evidence expose `current_tradeability=suspended`, `tradeable`, or an explicit `unknown` without silently changing state at later boundaries.
- A suspended snapshot reports authoritative status provenance and the correct latest genuinely traded close, distinct from the carried suspension close.
- History Store Degradation remains observable and preserves the live candidate's status; it does not blend stored and live history.
- A confirmed suspended BaoStock candidate is not bypassed through later fallback.
- This ticket preserves ADR-0023 evidence semantics but does not yet claim the final non-directional terminal outcome supplied by Ticket 02.

## Acceptance evidence

- Focused test output showing the same authoritative status/provenance at provider-bundle, snapshot, degraded-live, canonical-evidence, and audit boundaries.
- A deterministic audit fixture showing `current_tradeability=suspended` and the expected latest genuinely traded close separately from the carried suspension close.
- Provider-call trace proving the selected complete BaoStock candidate stops fallback, plus traded and status-unknown control traces.

## Regression tests

- [ ] A real accepted BaoStock suspension candidate reaches snapshot, canonical evidence, and audit with authoritative status and the correct latest genuinely traded close.
- [ ] An authoritative traded latest session yields `current_tradeability=tradeable` with unchanged 20-trading-session semantics.
- [ ] A blank or zero-volume row without authoritative status is not promoted to a Suspension Observation.
- [ ] History Store Degradation preserves the selected live candidate's status and provenance.
- [ ] A confirmed suspended complete candidate stops sequential fallback; no extra provider is called for a directional alternative or replay preparation.
- [ ] Contradictory or dropped status at snapshot construction fails closed with a typed validation result.

## Compatibility or migration requirements

- Preserve existing Mainland Instrument Identity, provider order, Adjustment Basis, Raw Market Observations, and immutable reports.
- Do not relabel historical status or rewrite existing snapshots. New fields must remain readable alongside legacy evidence that lacks authoritative status.
- Any unavailable legacy status remains explicitly unknown; migration must not infer or repair it.

## Acceptance criteria from the specification

- AC1 — suspension propagation, audit provenance, and latest genuinely traded close portions.
- AC2 — authoritative traded status, provider ordering, and mainland horizon compatibility portions.
- AC13 — focused/full-suite, lint, and no-unrelated-refactoring requirements.

## Verification commands

```powershell
pytest -q tests/test_baostock_data.py tests/test_current_tradeability.py tests/test_market_history_shadow.py tests/test_market_history_calendar_maintenance.py tests/test_evidence_chain_regressions.py
pytest -q
ruff check .
git diff --check
```

## Definition of done

- [ ] Production behavior carries authoritative BaoStock status and latest genuinely traded close through every specified boundary.
- [ ] Focused regressions prove live, degraded, traded, suspended, unknown, contradiction, and fallback behavior.
- [ ] Audit output is deterministic and uses the glossary's Current Tradeability and Suspension Observation terms.
- [ ] ADR-0023 evidence semantics and all parent-spec compatibility/out-of-scope constraints are preserved.
- [ ] Focused tests, the full supported suite, Ruff, and diff validation pass.

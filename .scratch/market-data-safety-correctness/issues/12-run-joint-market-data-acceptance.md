# 12 — Run joint market-data safety and point-in-time acceptance

Status: ready-for-agent

Blocked by: 01, 02, 03, 04, 05, 06, 07, 08, 09, 10, 11

## Parent specification

[Market-data trading safety and point-in-time correctness](../spec.md). Every cross-cutting invariant, compatibility/migration requirement, acceptance criterion, verification stage, and Out of Scope constraint is binding. [ADR-0023](../../../docs/adr/0023-block-decisions-for-current-mainland-suspensions.md) and [ADR-0031](../../../docs/adr/0031-coordinate-provider-requests-centrally.md) must both pass final review unchanged.

## Problem being solved

Each tracer bullet can pass alone while an integration seam still weakens trading safety, exact point-in-time membership, durability, asset configuration, retry accounting, compatibility, or worktree hygiene. The complete implementation needs one deterministic CLI/audit, migration, concurrency, and repository-quality acceptance pass.

## What to build

Run the full parent-spec acceptance matrix across Tickets 01–11 and make only minimal integration corrections required to satisfy it.

- Exercise suspended and tradeable mainland CLI paths, including zero model calls and no directional-memory writes for current suspension.
- Exercise v2 identity, exact pin publication/replay, OPIT incremental refresh, as-of correction cutoffs, and legacy-v1/database/report compatibility.
- Run the complete deterministic sidecar-mutex publication/GC process matrix plus corruption, timeout, backup/restore, and migration controls.
- Exercise custom crypto rejection, standard pinned `SOL-USD` resolution, asset-before-graph behavior, checkpoint compatibility, calendar-horizon audit, and mainland compatibility.
- Run final Yahoo-backed crypto CLI acceptance only after Ticket 10, proving exact physical-attempt accounting, cooldown, single-flight, retry cap, and sequential fallback.
- Capture Git status before and after the default-path runtime smoke and prove only intentionally governed output differs.
- Run focused suites, the full supported suite, Ruff, and whitespace/diff validation.
- Perform Spec Compliance review before Code Quality review and classify any remaining finding as Critical, Important, or Minor.

## Explicit non-goals

- Do not reopen diagnosis, redesign any accepted protocol, change Strategy Rules, expand the Supported Crypto Universe, reorder/fan out providers, or change Data Usage Mode.
- Do not run live paid model/provider acceptance; deterministic fixtures are sufficient and required.
- Do not bulk rewrite legacy IDs, pins, databases, payloads, checkpoints, reports, or runtime files.
- Do not perform broad cleanup, renames, dependency upgrades, formatting churn, or unrelated refactoring.
- Do not conceal failures by weakening assertions or excluding supported tests.

## Blocking dependencies

All implementation tickets block this ticket:

- Ticket 01 — authoritative BaoStock status propagation.
- Ticket 02 — ADR-0023 Decision Gate.
- Ticket 03 — v2 identity/exact pins and canonical database path.
- Ticket 04 — exact-member OPIT provenance.
- Ticket 05 — selected sidecar mutex implementation.
- Ticket 06 — deterministic concurrency/recovery evidence.
- Ticket 07 — shared crypto universe validation.
- Ticket 08 — shipped pinned crypto registry.
- Ticket 09 — asset configuration before graph/checkpoint/stream.
- Ticket 10 — ADR-0031 Yahoo retry ownership and final Yahoo-backed crypto CLI gate.
- Ticket 11 — exact default runtime ignore behavior.

## External behavior and audit contract

- Suspended mainland CLI output is the exact non-directional ADR-0023 Analysis Outcome with authoritative status, latest genuinely traded close, v2 identity, no directional fields, zero downstream model calls, and no memory write.
- Tradeable mainland behavior retains provider order, qfq Adjustment Basis, and 20-trading-session semantics.
- Every decision/replay audit binds to exact v2 membership and correct provenance; unsafe legacy state fails closed without rewriting history.
- Every committed payload reference resolves to digest/length-valid bytes across publication, GC, interruption, timeout, backup, restore, and process termination.
- Standard `SOL-USD` audit identifies the pinned registry/digest, Crypto Instrument Identity, `CCC` Reference Market, crypto Capability Profile, consecutive-daily calendar, and `20:calendar_days`.
- Yahoo audit physical-attempt events exactly match network attempts and preserve shared cooldown, single-flight, budget, and sequential fallback under ADR-0031.
- Default runtime files do not dirty the Git-status baseline while intentional files remain visible.

## Acceptance evidence

- A concise acceptance record mapping AC1–AC13 to passing deterministic commands and captured CLI/audit or persisted-state evidence.
- Legacy migration/compatibility results and the complete payload concurrency case matrix.
- Before/after Git-status comparison, full-suite and Ruff output, diff validation, and separate Spec Compliance/Code Quality review results.

## Regression tests

- [ ] AC1–AC2 mainland suspended/tradeable CLI and audit scenarios pass.
- [ ] AC3 exact v2 identity/pin collision and replay scenarios pass.
- [ ] AC4–AC5 OPIT/backfill/as-of scenarios pass.
- [ ] AC6 full deterministic payload concurrency/recovery matrix passes repeatedly.
- [ ] AC7 standard `SOL-USD` CLI/audit scenario passes.
- [ ] AC8 unsupported custom candidate and rollback scenario passes.
- [ ] AC9–AC10 Yahoo physical-attempt, retry, cooldown, single-flight, and fallback scenarios pass.
- [ ] AC11 representative legacy snapshot/pin/database/payload/checkpoint/report fixtures pass without destructive rewriting.
- [ ] AC12 clean-worktree runtime smoke and negative ignore controls pass.
- [ ] CLI and programmatic contracts match for the same deterministic evidence.
- [ ] All focused suites and the complete supported suite pass with no live market data or paid model.

## Compatibility or migration requirements

- Open representative legacy v1 IDs, valid/invalid pins, OPIT/backfill bundles, databases, payload files, checkpoints, and reports.
- Prove migrations are explicit, transactional, foreign-key checked, and failure-atomic; F5 creates only its runtime sidecar and does not alter the main schema.
- Prove no migration/open path triggers GC, destructive repair, bulk re-keying, relabeling, payload rewrite, report rewrite, request-state reset, or checkpoint reinterpretation.
- Prove valid legacy material stays readable and inconsistent material returns a typed fail-closed/degraded result.

## Acceptance criteria from the specification

- AC1 through AC13, jointly and without waiver.

## Verification commands

```powershell
pytest -q tests/test_baostock_data.py tests/test_current_tradeability.py tests/test_graph_trust_boundaries.py tests/test_decision_audit.py tests/test_memory_log.py
pytest -q tests/test_market_history_store.py tests/test_market_history_reconstruction.py tests/test_market_history_shadow.py tests/test_market_history_calendar_maintenance.py tests/test_mainland_history_equivalence.py
pytest -q tests/test_crypto_identity_registry.py tests/test_crypto_identity_registry_refresh.py tests/test_crypto_asset_mode.py tests/test_production_strategy_registry.py tests/test_cli_symbol_handling.py tests/test_env_overrides.py
pytest -q tests/test_provider_request_coordinator.py tests/test_vendor_errors.py tests/test_acquisition_controller.py tests/test_reporting.py
pytest -q -k "market_history and gitignore"
$before = git status --porcelain
pytest -q -k "clean_worktree or default_runtime"
$after = git status --porcelain
Compare-Object $before $after
pytest -q
ruff check .
git diff --check
```

## Definition of done

- [ ] AC1–AC13 pass with deterministic CLI, decision-audit, migration, concurrency, and clean-worktree evidence.
- [ ] ADR-0023 and ADR-0031 pass Spec Compliance review without reinterpretation.
- [ ] Every compatibility/out-of-scope constraint is verified and no historical/runtime material is destructively rewritten.
- [ ] No Critical or Important review finding remains; any Minor finding is either fixed in scope or explicitly documented without weakening acceptance.
- [ ] Focused tests, repeated deterministic concurrency tests, the full supported suite, Ruff, and diff validation pass.

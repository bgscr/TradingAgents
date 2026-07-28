# 09 — Resolve crypto asset configuration before graph construction

Status: ready-for-agent

Blocked by: 08

## Parent specification

[Market-data trading safety and point-in-time correctness](../spec.md). Its cross-cutting invariants, compatibility requirements, acceptance definitions, Supported Crypto Universe, and Out of Scope section are binding.

## Problem being solved

The CLI and programmatic path must know the authoritative Instrument Identity and Capability Profile before constructing graph, evidence, checkpoint, or stream state. Otherwise a supported Crypto Instrument can inherit equity-only semantics, including the wrong observation calendar and return horizon.

## What to build

Resolve one immutable run asset configuration from Ticket 08's pinned registry before graph construction, evidence/checkpoint schema selection, or streaming.

- Use instrument kind `crypto`, `CCC` Reference Market, the crypto Capability Profile, and the registered crypto calculation/Strategy Rules for supported Crypto Instruments.
- Compute the 20-day return from 21 consecutive daily closes including weekends and audit it as `20:calendar_days`; no equity `trading_days` horizon may appear.
- Include asset configuration, registry revision/digest, Reference Market, observation-calendar kind, and rule horizon in checkpoint/configuration signatures and decision audit.
- Make CLI and programmatic entry points produce the same terminal and audit contract for identical deterministic evidence.
- Fail closed before model work for unresolved/mismatched registry or asset configuration.
- Preserve mainland Instrument Identity, Current Analysis Provider Chain, qfq Adjustment Basis, Capability Profile, and 20-trading-session semantics.

## Explicit non-goals

- Do not expand the Supported Crypto Universe, add executable venues, or change Strategy Rules.
- Do not move Yahoo retry ownership; Ticket 10 owns provider-attempt coordination.
- Do not claim final Yahoo-backed crypto CLI acceptance until Ticket 10 is complete; use deterministic provider/model fixtures here.
- Do not silently migrate or reinterpret equity checkpoints as crypto checkpoints.
- Do not refactor unrelated graph or CLI architecture.

## Blocking dependencies

Blocked by Ticket 08. Ticket 10 independently blocks the Yahoo-backed portion of final crypto CLI acceptance in Ticket 12. This ticket also blocks Ticket 12.

## External behavior and audit contract

- A deterministic standard-config `SOL-USD` CLI run enters crypto mode before graph/checkpoint/stream construction.
- Decision audit identifies pinned registry ID/digest, canonical Crypto Instrument Identity, instrument kind `crypto`, `CCC` Reference Market, crypto Capability Profile, consecutive-daily observation calendar, and `20:calendar_days` horizon.
- The run uses 21 consecutive closes including weekends; it never exposes an equity `trading_days` horizon.
- CLI and programmatic contracts match for the same evidence.
- Unsupported or incoherently configured crypto fails non-directionally before model work.
- Mainland control runs retain their established 20-trading-session semantics and provider/adjustment behavior.

## Acceptance evidence

- Deterministic `SOL-USD` CLI and decision-audit output showing pinned registry/digest, `CCC`, crypto Capability Profile, consecutive-daily calendar, and `20:calendar_days`.
- Construction-order spy evidence showing asset configuration exists before graph, evidence, checkpoint, and stream creation.
- Matching programmatic result plus a mainland control audit showing unchanged 20-trading-session/qfq semantics.

## Regression tests

- [ ] CLI resolves `SOL-USD` asset configuration before graph, evidence, checkpoint, and stream creation.
- [ ] Crypto graph excludes inapplicable equity-only behavior and uses the crypto Capability Profile.
- [ ] Registered calculation consumes 21 consecutive daily closes including weekends and audits `20:calendar_days`.
- [ ] Checkpoint/configuration signature changes when asset configuration, registry pin, Reference Market, calendar kind, or horizon changes.
- [ ] Legacy checkpoint lacking proven asset semantics is diagnostic-only or starts a new safe run; it is not resumed directionally.
- [ ] CLI/programmatic terminal and audit contracts match.
- [ ] Mainland controls retain identity, provider order, qfq semantics, and 20-trading-session behavior.
- [ ] Missing/mismatched crypto configuration fails typed with zero model calls.

## Compatibility or migration requirements

- Do not rewrite legacy checkpoints or reports. Legacy checkpoints that cannot prove asset semantics are not resumed into directional execution without an explicit migration; default to a new run.
- New reports add asset/registry/calendar/horizon fields; legacy reports remain viewable but cannot satisfy new acceptance without a new run.
- Preserve mainland checkpoints/configuration and all existing Instrument family isolation.

## Acceptance criteria from the specification

- AC2 — mainland horizon/provider compatibility control.
- AC7 — complete deterministic standard-config crypto CLI/audit behavior except final Yahoo-backed integration.
- AC11 — checkpoint/configuration/report compatibility portions.
- AC13 — focused/full-suite, lint, no Strategy Rule change, and no-unrelated-refactoring requirements.

## Verification commands

```powershell
pytest -q tests/test_crypto_asset_mode.py tests/test_production_strategy_registry.py tests/test_crypto_identity_registry.py tests/test_cli_symbol_handling.py tests/test_decision_audit.py tests/test_env_overrides.py tests/test_graph_trust_boundaries.py
pytest -q
ruff check .
git diff --check
```

## Definition of done

- [ ] Asset configuration is authoritative and immutable before all graph/evidence/checkpoint/stream construction.
- [ ] `SOL-USD` produces the required deterministic crypto audit and calendar horizon.
- [ ] CLI/programmatic contracts match and incoherent configuration fails before model work.
- [ ] Legacy checkpoint/report and mainland compatibility requirements are preserved.
- [ ] Focused tests, the full supported suite, Ruff, and diff validation pass.

# Separate Advisory Commentary and typed diagnostics

Status: completed

Blocked by: 17

## Problem

One reviewed run labeled trader prose `FINAL TRANSACTION PROPOSAL: SELL` and later
published a validated `Buy`. The validated rating was correct, but the primary
log did not identify the trader output as non-directional Advisory Commentary.
Audits also showed admitted gates with `deterministic_gate_rejected` and
`snapshot_unavailable` despite successful BaoStock fallback.

## Settled direction

Raw model output remains preserved as an artifact. Operator-owned log/report
projections label it Advisory Commentary. Gate and audit diagnostics use typed
codes produced at the responsible boundary rather than substring inference.

## Acceptance

- Trader, debate, risk, and memory prose is visibly labeled Advisory Commentary
  wherever shown to an operator.
- `Final decision ready: <rating>` is reserved for the validated Trading Decision
  contract.
- Advisory `Sell` and validated `Buy` can coexist without ambiguity; only `Buy`
  enters decision, signal, or memory consumers.
- An admitted gate cannot report `deterministic_gate_rejected`.
- `snapshot_unavailable` appears only when no Authoritative Market Snapshot is
  available.
- AkShare failure plus BaoStock success is represented by acquisition/fallback
  telemetry and appropriate degradation, not a snapshot blocker.
- Raw artifacts remain lossless and content-addressed.

## Implementation boundaries

- Replace gate/audit substring classification with closed typed diagnostics.
- Do not rewrite or discard raw model output.
- Do not let Advisory Commentary influence the Validated Decision Context.

## Required tests

- Advisory Sell plus validated Buy end-to-end log/report test.
- Admitted gate diagnostic invariant.
- AkShare-to-BaoStock fallback diagnostic test.
- Raw artifact preservation test.
- Decision/signal/memory isolation test.

## Comments

Follow-up to ADR-0015 and the reviewed `601658.SS` run.

## Resolution

- Operator logs and reports now label trader, debate, risk, and memory prose as
  Advisory Commentary while preserving the raw content in content-addressed
  artifacts.
- Only the validated Trading Decision may emit `Final decision ready`; advisory
  direction cannot enter decision, signal, position, or Trading Memory
  consumers.
- Gate and audit projections use typed diagnostics rather than substring
  inference. Admitted gates cannot claim deterministic rejection, and
  `snapshot_unavailable` is emitted only when no Authoritative Market Snapshot
  exists.
- AkShare failure followed by BaoStock success is represented as fallback
  telemetry/degradation without a contradictory snapshot blocker.
- Advisory-Sell/validated-Buy, raw-artifact preservation, diagnostic invariant,
  fallback, and downstream isolation regressions pass.

Verification:

- Full suite: 1,392 passed, 2 expected skips, 71 subtests passed.
- Ruff and `git diff --check` passed.

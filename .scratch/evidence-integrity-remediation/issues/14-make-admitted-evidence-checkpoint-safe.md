# Make admitted evidence checkpoint-safe and run-scoped

Status: completed

Blocked by: 09, 10

## Problem

`DecisionPolicyEngine` retains trusted and admitted Evidence State in mutable
process memory. Starting a run clears prior admission bindings. A checkpoint
resume that skips Evidence Admission therefore loses the binding, and concurrent
runs can overwrite one another.

## Settled direction

The policy engine remains immutable configuration. A closed, versioned admitted
evidence binding travels in graph state and through checkpoints. The Decision
Gate recomputes and validates the binding plus every existing artifact, fact,
calculation, rule, assertion, and date check.

## Acceptance

- Resume from immediately after Evidence Admission with a fresh graph/policy
  instance produces the same normalized terminal contract as an uninterrupted
  run.
- Two concurrent run IDs cannot clear, replace, or consume one another's
  admitted evidence.
- The binding covers run identity, evidence semantic digest, context ID,
  registry digest, calculation-registry digest, and Evidence Integrity Status.
- Evidence, context, registry, calculation, and cross-run binding mutations fail
  closed.
- Current-schema JSON checkpoint round trips succeed.
- An old graph/checkpoint signature is explicitly migrated or rejected.
- No per-run mutable evidence remains on `DecisionPolicyEngine`.

## Implementation boundaries

- Primary modules: `tradingagents/decision_policy.py`,
  `tradingagents/graph/evidence_gate.py`,
  `tradingagents/graph/trading_graph.py`, graph state/propagation contracts.
- Preserve all existing deterministic Decision Gate validation.
- Do not add a process-global cache or a graph-instance dictionary keyed by run.

## Required tests

- SQLite checkpoint resume after admission.
- Fresh-process resume.
- Concurrent distinct-run isolation.
- Binding JSON round trip.
- Binding and evidence mutation negatives.
- CLI/programmatic normalized-terminal equivalence.

## Comments

Follow-up to the 2026-07-22 review and issue 10.

## Resolution

- Replaced process-memory admission trust with a closed, versioned admitted
  evidence binding carried in graph state and serialized through checkpoints.
- The binding covers run identity, evidence semantic digest, validated context,
  rule and calculation registries, and Evidence Integrity Status. The Decision
  Gate recomputes and validates those values before permitting a decision.
- `DecisionPolicyEngine` now remains immutable policy configuration; concurrent
  runs do not clear, replace, or consume one another's admission state.
- SQLite resume, fresh-graph resume, concurrent-run isolation, JSON round trip,
  binding/evidence mutation negatives, signature rejection, and normalized
  CLI/programmatic terminal-equivalence regressions pass.

Verification:

- Full suite: 1,392 passed, 2 expected skips, 71 subtests passed.
- Ruff and `git diff --check` passed.

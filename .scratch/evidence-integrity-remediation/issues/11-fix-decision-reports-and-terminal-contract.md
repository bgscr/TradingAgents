# Fix decision reports, metrics, and terminal contracts

Status: completed

Blocked by: 10

Render human-readable decisions from the validated contract, split coverage and integrity measures, remove uncalibrated confidence, separate lifecycle/outcome/integrity status, add canonical run/export identity, and persist stage-level cost/call/retry telemetry.

## Acceptance

- Reports contain semantic fact labels, rule/predicate details, lineage, integrity, coverage, and degradation rather than bare values.
- Advisory model prose remains visibly separate and non-directional.
- Completed decisions and completed Analysis Outcomes have distinct terminal kinds.
- Completion alone cannot trigger signal or memory consumers.
- Saved exports identify their canonical run and audit digest.

## Comments


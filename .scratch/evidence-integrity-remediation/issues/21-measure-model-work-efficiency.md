# Measure model-work efficiency

Status: completed

Blocked by: 17, 18, 19

## Problem

Each reviewed run made 40 model calls and used hundreds of thousands of input
tokens while the validated direction came from one deterministic return fact.
All analyst material claims were unsupported. This is expensive and confusing,
but changing the workflow before telemetry and trust boundaries are correct
could hide useful work or weaken assurance.

## Decision to make

After tickets 17 through 19, decide whether research depth should:

1. retain all Advisory Commentary stages with explicit budgets;
2. shorten optional stages when they cannot add a registered Decision Assertion;
3. offer a deterministic-decision-only execution mode; or
4. remain unchanged after measuring operator value.

## Evidence required for triage

- Model calls, tokens, cost availability, and elapsed time by stage.
- Unsupported/material claim counts by stage.
- Operator-usefulness assessment for Advisory Commentary.
- Comparison across shallow, medium, and deep research configurations.
- Proof that any proposed optimization preserves all gates, audits, reports, and
  terminal contracts.

## NOT in scope before triage

- Removing gates or deterministic reporting.
- Letting model prose authorize direction.
- Treating fewer tokens as evidence of better decisions.
- Combining topology changes with P0/P1 remediation.

## Measured baseline

The corrected telemetry and trust contracts are available for four completed
equity Trading Decisions. All four used the same deep graph topology:
`debate=5|risk=5`.

| Instrument | Artifact run ID | Model calls | Input tokens | Output tokens | Elapsed |
| --- | --- | ---: | ---: | ---: | ---: |
| `600895.SS` | `20260723_054224` | 40 | 442,801 | 82,913 | 985 s |
| `600895.SS` | `20260723_165145` | 41 | 458,418 | 87,798 | 948 s |
| `601658.SS` | `20260723_052321` | 42 | 548,384 | 82,537 | 1,095 s |
| `601658.SS` | `20260723_170835` | 41 | 443,555 | 86,141 | 967 s |

Across these runs:

- Model calls ranged from 40 to 42, with a median of 41.
- Median input use was 450,986.5 tokens and mean input use was 473,289.5
  tokens.
- Median output use was 84,527 tokens and mean output use was 84,847.25
  tokens.
- Median elapsed time was 976 seconds and mean elapsed time was 998.75
  seconds.
- Cost telemetry was unavailable for every run and remained
  `available=false`, `amount_usd=null`; no zero-cost value was fabricated.

Average model work by stage:

| Stage | Calls | Input tokens | Output tokens | Model time |
| --- | ---: | ---: | ---: | ---: |
| Analysis | 15 | 157,639.50 | 37,694.25 | 304.55 s |
| Research debate | 11 | 131,984.75 | 24,801.25 | 317.00 s |
| Risk debate | 14 | 181,817.25 | 21,024.75 | 252.79 s |
| Trading | 1 | 1,848.00 | 1,327.00 | 16.35 s |

The audits contain 143 model-authored Material Claims. All 143 are unsupported
and excluded from the Validated Decision Context. Each Trading Decision uses
one validated Source Fact and one registered Decision Assertion. The result is
trustworthy because Advisory Commentary is isolated, but nearly all measured
model work produces no validated claim.

## Comparison limitation

Historical shallow `debate=1|risk=1` runs predate the corrected telemetry and
trust contracts. They do not contain comparable stage/token data. No corrected
medium baseline exists, and no operator-usefulness assessment has been
performed. Comparing those historical runs to this deep baseline would create
false precision.

Obtaining a matched shallow/medium/deep comparison requires paid model and
networked provider calls. That work belongs inside the explicitly authorized,
bounded live-validation gate in ticket 13; it is not run automatically for this
triage.

## Triage decision

Retain the current topology unchanged for now.

This is a conservative decision, not evidence that the topology is efficient.
The current data strongly identifies Advisory Commentary as the optimization
target, but it cannot distinguish which stages operators find useful or prove
that a shallower topology preserves that value. No gate, audit, deterministic
report, terminal contract, signal boundary, or memory boundary is weakened.

A future optimization proposal must:

1. run matched shallow, medium, and deep configurations against the same
   Instrument Identity, requested date, Authoritative Market Snapshot, selected
   analysts, models, and evidence inputs;
2. record the telemetry and trust outcomes listed above;
3. include an operator-usefulness assessment for Advisory Commentary; and
4. treat live-model prose equality as neither a correctness oracle nor a reason
   to change a Trading Decision.

## Comments

Follow-up to the 2026-07-22 runtime-efficiency observation.

Triaged on 2026-07-23 from four corrected deep-run audits and runtime-metrics
files. No model, provider, or network call was made for this triage.

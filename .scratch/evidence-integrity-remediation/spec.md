# Evidence integrity and runtime efficiency remediation

Status: ready-for-agent

## Source

This specification incorporates:

- the audit in `docs/2026-07-16-recent-code-reports-logs-audit.md`;
- the persisted `600895.SS`, `601658.SS`, and `510500.SS` runs from 2026-07-19;
- the domain contract in `CONTEXT.md`; and
- the decisions recorded in `docs/adr/0001` through `0016`.

## Objective

TradingAgents may publish a directional Trading Decision only when every premise permitted to influence direction is represented by a valid, rule-backed Decision Assertion over canonical Source Facts. Invalid, unavailable, insufficient, or conflicted evidence must fail closed without being disguised as `Hold`. Deterministic blockers must stop expensive model work as early as possible, while loss of optional evidence degrades breadth without automatically blocking.

## Trusted data flow

```text
Raw provider response
  -> typed Source Acquisition Outcome
  -> immutable Source Artifact
  -> deterministic source adapter
  -> canonical Source Facts and Calculation Lineage
  -> Evidence Preflight
  -> analyst fact/rule selection
  -> Evidence Admission
  -> Validated Decision Context
  -> final direction selector
  -> Decision Gate
  -> Trading Decision or Analysis Outcome
  -> deterministic report and terminal status
```

Narrative reports, debate transcripts, model memory, operational error text, and unvalidated quotes remain outside the directional trust path.

## Required behavior

### Canonical evidence contracts

- Preserve raw provider artifacts losslessly and address them by SHA-256.
- Use versioned, closed Pydantic v2 models at trusted seams. Forbid unknown fields and validate constrained domain scalars and local cross-field invariants.
- Do not apply blanket strict Python-mode validation to JSON-shaped graph/checkpoint dictionaries. Use explicit JSON serialization adapters and schema migrations.
- Declare Pydantic directly in project dependencies and reject unsupported major versions.
- A Source Fact records stable identity, canonical field, typed normalized value and unit, Instrument Identity, effective date/time, adjustment basis where applicable, provenance, source span or canonical record path, and calculation lineage where applicable.
- Fact identity derives from canonical source identity, artifact digest, and canonical record path/span. It must not depend on model wording or a runtime tool-call ID.
- Models may select registered fact and rule identifiers but may not define fact meaning or author directional rules.

### Source acquisition

- Every provider attempt returns a typed Source Acquisition Outcome: available artifact or unavailable reason with provider, capability, attempt, timestamp, retryability, and typed diagnostics.
- Rate limits, timeouts, authentication failures, configuration failures, no-data responses, and malformed payloads cannot become Source Facts or available evidence sources.
- Deterministic acquisition policy owns provider fallback, retry limits, backoff, `Retry-After`, and per-run circuit breakers. Models cannot force immediate retries by rephrasing requests.
- Optional-source exhaustion produces Degraded Evidence. Required-capability exhaustion blocks at the earliest applicable gate.
- Preserve original Chinese source text and extract source-linked monetary facts before model consumption. Normalize `元`, `万元`, and `亿元`, including ranges, without replacing the raw expression.

### Instruments and capabilities

- Instrument Identity requires authoritative canonical symbol, trading venue, instrument kind, currency, and provenance. Display name is optional metadata.
- Resolve explicit mainland exchange suffixes authoritatively and distinguish equities, funds, and indices using a market-specific registry rather than ticker-shape guessing.
- Instrument kind selects a Capability Profile controlling required evidence, available analysts, and applicable Strategy Rules.
- Funds use fund-specific benchmark, NAV/premium, holdings/exposure, tracking, and liquidity semantics and never inherit company-fundamentals requirements merely from a legacy stock label.

### Authoritative snapshots and derived facts

- Validate OHLCV schema, numeric coercion, `Low <= Open/Close <= High`, nonnegative volume, valid dates, duplicate-date policy, staleness, provider, retrieval time, adjustment basis, and effective trading date.
- Quarantine invalid provider data and continue through the configured provider chain.
- Select one Authoritative Market Snapshot for exact prices and all internally derived indicators in an analysis.
- Every derived fact references a versioned Calculation Definition specifying inputs, formula, adjustment basis, missing-value policy, history/warmup requirement, implementation version, output unit, and precision.
- Calculation Lineage binds the fact to the exact input artifact, effective range, observation count, calculation version, and deterministic result digest.
- When required history is unavailable, return `insufficient_history`; never expose a shorter-window value under a longer-window field identity.
- External vendor indicators remain distinct source facts and cannot impersonate registered internal calculations with unknown lineage.

### Three fail-closed graph gates

- Put all gates inside the compiled graph so CLI and programmatic execution share the same routing and terminal validation.
- Evidence Preflight runs before model-mediated analysis and validates input shape, Instrument Identity, Authoritative Market Snapshot, effective date, adjustment basis, digest, and baseline history. A blocked preflight emits an Analysis Outcome and invokes no analyst/model stages.
- Evidence Admission runs after evidence collection and before thesis synthesis. It requires adapter completion, fact/artifact integrity, required source status, and at least one decision-ready rule/assertion candidate.
- The final direction selector receives only a Validated Decision Context. Research, trader, risk, memory, and advisory prose cannot enter that interface.
- The Decision Gate verifies closed-ledger membership, unique and complete assertion coverage, applicable Strategy Rules, fact/rule compatibility, and permitted direction exposure.
- A blocked Decision Gate publishes no Trading Decision, signal, position data, or decision-memory update.
- Shadow diagnostics may be retained, but shadow output is always non-directional and cannot be consumed as a Trading Decision.
- Increment the evidence/checkpoint schema signature and do not resume incompatible old checkpoints without an explicit migration.

### Directional semantics

- Every directional Decision Assertion identifies a versioned Strategy Rule and the Source Facts to which it applies.
- A Strategy Rule declares applicable instrument kinds, required canonical fields, comparator/threshold, predicate, polarity, horizon, freshness, history, and missing/conflict behavior.
- Deterministic validation recomputes rule applicability and predicate results from registered facts.
- Models may select fact/rule identifiers and provide Advisory Commentary. They cannot invent a directional rule.
- If no applicable registered rule supports direction, finish with an Analysis Outcome.
- `Hold` remains a genuine rule-backed portfolio recommendation, never a missing-data sentinel.

### Assurance, reporting, and terminal state

- Replace overloaded evidence coverage and uncalibrated Decision Confidence with Source Availability Coverage, Validated Fact Coverage, Decision Assertion Coverage, and Evidence Integrity Status.
- Duplicated artifacts, facts, or sources cannot inflate coverage. Decision Assertion Coverage must be complete for a Trading Decision.
- Do not display predictive Decision Confidence unless it is calibrated against out-of-sample outcomes.
- Render Decision Reports deterministically from the validated contract, including canonical labels, values/units, dates, Strategy Rules, predicates, polarity, horizon, provenance, calculation lineage, coverage, and degradation.
- Keep model-authored Advisory Commentary separate and non-directional.
- Separate Run Lifecycle Status, Terminal Outcome Kind, and Evidence Integrity Status. Completion alone cannot authorize signals or memory writes.
- A completed Analysis Outcome is not an operational failure. Terminal runs have no active phase, and operational failures carry typed error categories.
- Canonical run exports include a stable run ID, schema/rule/calculation versions, configuration digest, and audit digest so copies cannot be mistaken for distinct runs.
- Persist per-stage model/tool calls, tokens, cost where available, latency, retry/circuit-breaker events, and terminal routing.

### Decision metamorphic testing

- Use deterministic fixtures, fake model clients, graph call counters, and Hypothesis-backed generators at the same interfaces used by production callers.
- Invariance properties include permutation, duplication, irrelevant optional-source loss, equivalent unit/locale normalization, runtime call-ID changes, and unsupported/advisory prose mutation.
- Sensitivity properties include semantic field swaps, material value/sign/date changes, missing selected facts, cross-claim bindings, and arbitrary rating substitution.
- Preflight blocking must prove zero analyst/model calls; admission blocking must prove zero research/trader/risk calls; final blocking must prove zero decision/signal/memory writes.
- Recorded structured outputs may test prompt/schema regression. Live-model evaluation remains separate and must not assert byte-identical stochastic ratings or prose.
- CLI and programmatic paths must produce the same terminal contract for identical deterministic fixtures.

### Existing PIT and runtime-artifact scope

- Preserve the completed SQLite PIT-state migration, immutable manifests, content-addressed runtime artifacts, bounded background writer, and reference-aware offline garbage collection.
- Keep artifact I/O and garbage collection off the active analysis critical path.
- Do not weaken prior recoverability, audit references, or performance acceptance while changing evidence schemas.

## Acceptance matrix

| Scenario | Required result |
| --- | --- |
| `510500.SS` identity provider fails but authoritative fund registry resolves symbol/venue/kind | Fund Capability Profile; no company-fundamentals requirement |
| Required identity remains unresolved | Preflight Analysis Outcome; zero analyst/model calls |
| News returns HTTP 429 text | Typed unavailable outcome; no artifact-derived fact; circuit breaker prevents immediate retry |
| Snapshot contains 129 usable rows | No valid SMA-200 fact; `insufficient_history` lineage outcome |
| `0.592067` is relabeled from P/B to Beta | Canonical field/fact validation rejects the binding |
| P/E and RSI are offered for Underweight without an applicable Strategy Rule | Decision Gate blocks direction |
| Unsupported debate/report prose is reversed or made sensational | Identical Validated Decision Context and normalized decision contract |
| Facts, assertions, and source records are permuted | Identical normalized gate result and decision contract |
| Artifacts/facts are duplicated | No coverage inflation or directional change |
| A selected material fact's value, sign, or date changes | Prior assertion invalidates; recompute, change, or abstain |
| Same evidence is paired with arbitrary ratings | Only rule-supported directions can pass |
| Optional unused source becomes unavailable | Direction/assertions unchanged; availability/degradation only may change |
| JSON checkpoint round-trip under current schema | Boundary validation succeeds |
| Old checkpoint signature is resumed | Explicit migration or fail-closed rejection |
| Decision Gate blocks | No final decision, signal, position fields, or memory write |
| Completed non-directional run | `lifecycle_status=completed`, `terminal_outcome_kind=analysis_outcome` |
| CLI and programmatic runners receive identical fixture | Same terminal kind and validated contract |

Legacy acceptance scenarios for Chinese monetary normalization, invalid OHLC fallback, mainland suffix canonicalization, PIT performance, buffered artifacts, and full-suite verification remain in issues `01` through `08`.

## Rollout

1. Land strict canonical contracts, deterministic identifiers, typed acquisition outcomes, Instrument Identity/Capability Profiles, and calculation lineage behind compatibility adapters.
2. Add regression/metamorphic fixtures and establish failing acceptance tests for the 2026-07-19 artifacts.
3. Insert Evidence Preflight and strengthen Evidence Admission without exposing new directional output.
4. Land the Strategy Rule registry, isolate the final selector, and enforce the Decision Gate.
5. Quarantine directional shadow output and bump checkpoint/schema signatures.
6. Land deterministic reports, split metrics, terminal-state contract, export identity, and cost/call telemetry.
7. Run focused tests, full suite, Ruff, diff checks, replay fixtures, migration tests, and required specialist reviews.
8. Only with explicit cost approval, run controlled live validation for an equity, a mainland fund, and an optional-source-degraded case.

## Non-goals

- This remediation does not claim that rule-backed decisions are profitable; predictive calibration and walk-forward evaluation are separate quantitative work.
- Ranking, execution simulation, portfolio optimization, corporate-action accounting beyond snapshot adjustment, and position sizing remain outside this scope.
- The initial Strategy Rule catalog may be deliberately small; absence of a rule causes abstention rather than model invention.
- Providers are not queried merely to vote on already valid data.
- Live-model text or rating equality is not a test oracle.
- Automatic artifact retention limits remain deferred until storage-growth data exists.

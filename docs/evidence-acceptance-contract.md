# Evidence integrity acceptance contract

The following scenarios define the evidence and decision gates covered by the ordinary offline suite. Each row has a corresponding entry and test node in `tests/fixtures/evidence_integrity_acceptance_matrix.json`. Update both when the contract changes. Historical run records remain separate.

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

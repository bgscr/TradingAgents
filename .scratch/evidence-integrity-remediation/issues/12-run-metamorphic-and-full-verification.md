# Run metamorphic and full remediation verification

Status: ready-for-agent

Blocked by: 09, 10, 11

Add deterministic replay fixtures for the 2026-07-19 runs, property/metamorphic tests at trusted interfaces, graph call/write counters, checkpoint migration coverage, and CLI/programmatic equivalence. Run all focused and repository-wide verification plus required specialist reviews.

## Acceptance

- Every row in the spec acceptance matrix has deterministic coverage.
- Hypothesis covers permutation, duplication, canonical normalization, malformed references, and semantic counterexamples.
- Recorded prompt outputs test schema and fail-closed behavior without live-model equality assertions.
- Focused tests, full suite, Ruff, and `git diff --check` pass.
- Specialist Stage 1 spec-compliance and Stage 2 code-quality reviews report no unresolved Critical or Important findings.

## Comments


# Shadow replay and enable fail-closed enforcement

Status: completed

Replay the audited runs and a representative recent-run sample through the new evidence system in shadow mode. Review all unexpected blocks, document justified rule changes, and then enable enforcement by default.

Blocked by: 04

## Acceptance

- Known monetary, OHLC, and material provider/date conflicts would block.
- Valid runs with unavailable optional sources remain decision-capable with degraded coverage.
- Every unexpected block in the sample is reviewed before default enforcement.
- Any temporary legacy override is explicit and labels output as evidence-gate unenforced.

## Comments

# Run full remediation verification

Status: completed

Run all targeted regressions, the full PIT suite with the `pit` extra installed in an isolated environment, linting, diff checks, replay validation, and performance benchmarks before landing the complete remediation.

Blocked by: 05, 06, 07

## Acceptance

- Exact audit regressions pass.
- The full PIT suite runs with a Parquet engine; missing `pyarrow` is not reported as a source failure.
- Ruff and repository diff checks pass.
- Replay and performance results are attached to the implementation handoff.
- Unrelated user changes remain untouched.

## Comments

- Final cleanup: fix the three repository-wide Ruff violations currently reported in
  `tests/test_windows_launchers.py` (`I001` and two `UP022` findings), then rerun
  Ruff after all remediation tasks are complete.

## Verification — 2026-07-17

- Exact remediation regressions: `89 passed`.
- PIT selection in the working interpreter: `169 passed, 1 skipped`.
- PIT selection in `uv run --isolated --extra pit`: `169 passed, 1 skipped,
  827 deselected`; the sole skip is the live test gated by `TUSHARE_TOKEN`.
- Final-audit extension regressions: `9 passed` (run-scoped snapshots, mainland
  indices, source-content validation, full-decision numeric gating, and writer
  failure propagation).
- Complete repository suite after the final audit: `1005 passed, 1 skipped`.
- Windows launcher suite after the deferred cleanup: `8 passed`.
- Repository-wide Ruff: clean.
- `git diff --check`: clean.
- Replay evidence: `../shadow-replay-2026-07-17.md`.
- PIT benchmark evidence: `../pit-state-benchmark-2026-07-17.md`.
- Runtime-artifact benchmark evidence:
  `../runtime-artifacts-benchmark-2026-07-17.md`.

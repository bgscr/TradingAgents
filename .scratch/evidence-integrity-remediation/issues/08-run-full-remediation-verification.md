# Run full remediation verification

Status: ready-for-agent

Run all targeted regressions, the full PIT suite with the `pit` extra installed in an isolated environment, linting, diff checks, replay validation, and performance benchmarks before landing the complete remediation.

Blocked by: 05, 06, 07

## Acceptance

- Exact audit regressions pass.
- The full PIT suite runs with a Parquet engine; missing `pyarrow` is not reported as a source failure.
- Ruff and repository diff checks pass.
- Replay and performance results are attached to the implementation handoff.
- Unrelated user changes remain untouched.

## Comments

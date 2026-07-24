# Stabilize dependencies and Pydantic contracts

Status: completed

Blocked by: 18

## Problem

The committed change raised LangGraph and several provider/runtime dependency
floors beyond the evidence-remediation requirement. Pydantic 2.13 also warns
that an `after` validator returns `model_copy()` during direct construction, a
pattern that may stop working in a future release.

## Settled direction

Every dependency floor needs a demonstrated compatibility reason and automated
coverage. Contract normalization must use a Pydantic-supported validator path
and preserve direct construction plus JSON/checkpoint round trips.

## Acceptance

- Each changed dependency floor is justified by required behavior or reverted
  in an isolated change.
- Supported Python/Pydantic versions are explicit and exercised in CI.
- The affected model emits no warning during direct construction.
- Direct construction and JSON round trips preserve identical canonical values.
- Warnings-as-errors coverage protects the validator path.
- Windows launcher/PyInstaller smoke tests pass after dependency changes.

## Implementation boundaries

- Do not mix new provider features into compatibility cleanup.
- Do not weaken closed models or checkpoint validation to silence warnings.
- Keep dependency reversions separate from P0/P1 behavioral changes.

## Required tests

- Direct construction with warnings treated as errors.
- JSON/checkpoint round trip.
- Supported version matrix in CI.
- Windows launcher and packaging smoke coverage.

## Comments

Follow-up to the 2026-07-22 code review.

## Resolution

- Replaced the unsupported Pydantic `after` validator that returned
  `model_copy()` with a field validator. Direct construction and JSON round
  trips now preserve the same canonical acquisition-outcome order with
  warnings treated as errors.
- Kept only two raised floors with demonstrated compatibility requirements:
  `langgraph>=1.2.0` supplies the runtime contract required by the project and
  remains compatible with the resolved `langgraph-prebuilt`; and
  `hypothesis>=6.10.1` avoids the removed entry-point API that prevents the
  pytest plugin from loading on supported Python 3.13.
- Reverted the unexplained `tqdm`, `typing-extensions`, `akshare`, `baostock`,
  `yfinance`, and Ruff floor increases to their prior values.
- Added a Python 3.13 CI job that installs Pydantic 2.10.6 and the exact declared
  minimum dependency set, then runs the full suite and Ruff.
- Added a Windows CI job that builds the PyInstaller release, runs both portable
  launcher smoke checks, and starts the packaged CLI with `--help`.

Local verification:

- Exact minimum compatibility set: 132 focused tests passed.
- Launcher and PyInstaller entrypoint tests: 15 passed.
- Real Windows PyInstaller build: completed successfully.
- Packaged `tradingagents.exe --help`: exit code 0.

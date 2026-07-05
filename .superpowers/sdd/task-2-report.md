# Task 2 Report: PyInstaller Entrypoints and Spec

## Status

Implemented Task 2 packaging scaffolding:

- Created `packaging/pyinstaller/tradingagents_entry.py`.
- Created `packaging/pyinstaller/ak_pick_a_stock_entry.py`.
- Created `packaging/pyinstaller/tradingagents_portable.spec`.
- Created `tests/test_pyinstaller_entrypoints.py`.

The wrappers include a minimal repository-root `sys.path` insertion so the exact smoke commands from the brief work when the scripts are executed directly from `packaging/pyinstaller`.

## TDD Evidence

Red:

```text
rtk pytest tests/test_pyinstaller_entrypoints.py -q
Pytest: 0 passed, 2 failed
FileNotFoundError for missing tradingagents_entry.py and ak_pick_a_stock_entry.py
```

After creating the brief-specified wrappers and spec, the brief tests passed:

```text
rtk pytest tests/test_pyinstaller_entrypoints.py -q
Pytest: 2 passed
```

Smoke testing then exposed a direct-script execution import bug:

```text
rtk python packaging/pyinstaller/tradingagents_entry.py --help
ModuleNotFoundError: No module named 'cli'

rtk python packaging/pyinstaller/ak_pick_a_stock_entry.py
ModuleNotFoundError: No module named 'ak_pick_a_stock'
```

Regression red:

```text
rtk pytest tests/test_pyinstaller_entrypoints.py -q
Pytest: 2 passed, 1 failed
test_entrypoints_are_importable_from_script_directory failed because the repo root was not on sys.path.
```

Green:

```text
rtk pytest tests/test_pyinstaller_entrypoints.py -q
Pytest: 3 passed
```

## Verification

Targeted tests:

```text
rtk pytest tests/test_pyinstaller_entrypoints.py -q
Pytest: 3 passed
```

Lightweight import smoke:

```text
rtk python -c "...load both entrypoint files..."
entrypoint imports ok
```

TradingAgents help smoke:

```text
rtk python packaging/pyinstaller/tradingagents_entry.py --help
Exit code: 0
Printed Typer help.
```

Candidate-picker live smoke:

```text
rtk python packaging/pyinstaller/ak_pick_a_stock_entry.py
Exit code: 124
Timed out after 184036 ms with no ImportError or ModuleNotFoundError output.
```

The candidate-picker command did not reach a clear market-data error within the timeout. The previous import failure is fixed, and the targeted import smoke confirms both entrypoint modules import successfully.

## Self-Review

- Scope stayed within Task 2 owned files plus this report.
- No launcher, `pyproject`, build-script, or candidate-picker behavior changes were made.
- The spec includes `cli` data collection and both `cli` and `tradingagents` in hidden import collection as required.
- The PyInstaller collection name is `TradingAgents-Win64`.
- Pre-existing unrelated SDD files in the worktree were not modified or staged.

## Concerns

- The candidate-picker live smoke timed out after 184 seconds. This appears to be runtime/live-data behavior rather than a packaging import failure, but it did not emit a concrete market-data exception before timeout.

## Review Fix Follow-Up (2026-07-05)

### Issue Addressed

- `packaging/pyinstaller/tradingagents_entry.py` imported `cli.main` without first setting app-local portable defaults, so direct execution of `tradingagents.exe` could fall back to `%USERPROFILE%\.tradingagents`.
- `packaging/pyinstaller/ak_pick_a_stock_entry.py` imported `ak_pick_a_stock` before setting a packaged default output path.

### Red Phase

```text
rtk pytest tests/test_pyinstaller_entrypoints.py -q
Pytest: 5 passed, 2 failed
```

The new failing tests confirmed missing `TRADINGAGENTS_RESULTS_DIR` defaults and missing `AK_PICK_OUTPUT_PATH` before picker import.

### Fix

- Added wrapper-local app directory resolution:
  - frozen mode: `Path(sys.executable).resolve().parent`
  - source/script mode: repository root
- `tradingagents_entry.py` now sets default `TRADINGAGENTS_RESULTS_DIR`, `TRADINGAGENTS_CACHE_DIR`, and `TRADINGAGENTS_MEMORY_LOG_PATH` under the app directory before `cli.main` is imported.
- `ak_pick_a_stock_entry.py` now sets default `AK_PICK_OUTPUT_PATH` to `<app dir>\reports\ak_candidates.csv` before importing `ak_pick_a_stock`.
- Used `os.environ.setdefault` so future launcher-provided environment values are preserved.

### Verification

```text
rtk pytest tests/test_pyinstaller_entrypoints.py -q
Pytest: 7 passed
```

```text
rtk python packaging/pyinstaller/tradingagents_entry.py --help
Exit code: 0
Printed Typer help.
```

```text
rtk python -c "...load both entrypoint files..."
entrypoint imports ok
```

### Concerns

- No long live market-data smoke was run for `ak_pick_a_stock_entry.py`; verification was intentionally kept to import-only smoke per controller clarification.

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

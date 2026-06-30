# Task 1 Report: CLI Presets, Config, and Beijing-Date Validation

Status: DONE

Commit:
- 4436b0f feat: add China A-share preset selection

Files changed:
- `cli/main.py`
- `tradingagents/default_config.py`
- `tests/test_china_a_cli_enhancements.py`

Implementation summary:
- Added China mainland A-share ticker detection through `resolve_china_a_symbol`.
- Added CLI-only China A-share enhancement preset selection with numeric aliases and `basic` fallback behavior.
- Added `china_a_enhancement_preset` default config and propagated the selected preset into run config.
- Changed date prompting to use Beijing date limits for China mainland A-shares while preserving local-date validation for non-China symbols.
- Allowed same Beijing-date A-share analysis with an incomplete-data warning.
- Rejected dates beyond the relevant limit date with a max-date error message.

TDD evidence:
- RED preset run: `rtk pytest tests/test_china_a_cli_enhancements.py -q`
  - Result: 4 failed as expected due to missing `is_china_a_ticker`, missing `select_china_a_enhancement_preset`, and missing config key propagation.
- GREEN preset run: `rtk pytest tests/test_china_a_cli_enhancements.py::test_is_china_a_ticker_accepts_mainland_forms tests/test_china_a_cli_enhancements.py::test_is_china_a_ticker_rejects_non_mainland_forms tests/test_china_a_cli_enhancements.py::test_build_run_config_carries_china_a_preset tests/test_china_a_cli_enhancements.py::test_china_a_preset_prompt_maps_numeric_choice_to_value -q`
  - Result: 4 passed.
- RED date run: `rtk pytest tests/test_china_a_cli_enhancements.py::test_china_a_same_beijing_date_allowed_when_local_date_is_behind tests/test_china_a_cli_enhancements.py::test_china_a_after_beijing_today_is_rejected_then_accepts tests/test_china_a_cli_enhancements.py::test_non_china_symbol_keeps_local_date_limit -q`
  - Result: 3 failed as expected because `get_analysis_date()` did not accept a ticker yet.
- GREEN full Task 1 run: `rtk pytest tests/test_china_a_cli_enhancements.py -q`
  - Result: 7 passed.
- Required regression run: `rtk pytest tests/test_cli_symbol_handling.py tests/test_cli_env_skip.py tests/test_cli_config_precedence.py -q`
  - Result: 37 passed.
- Post-cleanup reruns:
  - `rtk pytest tests/test_china_a_cli_enhancements.py -q`: 7 passed.
  - `rtk pytest tests/test_cli_symbol_handling.py tests/test_cli_env_skip.py tests/test_cli_config_precedence.py -q`: 37 passed.

Environment note:
- The active Python environment initially lacked project runtime dependencies and pytest. I ran `rtk proxy py -m pip install -e .[dev]` to install the editable project with dev test dependencies before verification.

Concerns:
- None.

## Review Fix: Preset Default and A-Share-Only Predicate

Status: DONE

Commit:
- This fix commit.

Fix summary:
- Changed the China A-share enhancement interactive default from `flow_sentiment` to `basic`.
- Updated the Step 1b question box default label to `basic`.
- Tightened the CLI-only `is_china_a_ticker()` predicate so mainland B-share prefixes `900` and `200` do not trigger A-share presets or Beijing-date validation.

TDD evidence:
- RED run: `rtk pytest tests/test_china_a_cli_enhancements.py -q`
  - Result: 6 passed, 2 failed as expected.
  - Failures covered `900901.SS` being accepted and default prompt behavior returning `flow_sentiment`.
- GREEN run: `rtk pytest tests/test_china_a_cli_enhancements.py -q`
  - Result: 8 passed.
- Required regression run: `rtk pytest tests/test_cli_symbol_handling.py tests/test_cli_env_skip.py tests/test_cli_config_precedence.py -q`
  - Result: 37 passed.

Concerns:
- None.

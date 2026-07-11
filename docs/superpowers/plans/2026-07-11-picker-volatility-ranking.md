# Picker Volatility Ranking Correction Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Correct the A-share candidate picker so higher 20-day volatility receives a larger score penalty than lower volatility.

**Architecture:** Keep the existing percentile-based scoring pipeline and express factor direction at the call site: desirable factors add their normal ascending percentile rank, while volatility subtracts it. Remove the single-use `lower_is_better` branch and protect the intended ordering with one focused regression test.

**Tech Stack:** Python 3.10+, pandas, pytest, Ruff

## Global Constraints

- Do not change the volatility definition or annualize it.
- Do not change scoring weights, liquidity thresholds, valuation filters, or the top-ten output limit.
- Preserve missing-value behavior: values coerced to `NaN` receive a zero contribution after `fillna(0)`.
- Do not add configuration, dependencies, abstractions, or Beijing-date/data-path changes.
- Keep changes limited to `ak_pick_a_stock.py` and `tests/test_ak_pick_a_stock.py`.
- Complete Stage 1 spec-compliance review before Stage 2 code-quality review.

## File Structure

- `ak_pick_a_stock.py`: owns candidate factor calculation and weighted percentile scoring; modify `_rank_bonus()` and its volatility caller only.
- `tests/test_ak_pick_a_stock.py`: owns isolated picker regressions; add one test that holds all non-volatility factors constant and verifies the volatility penalty direction.

---

### Task 1: Correct the volatility penalty direction

**Files:**
- Modify: `ak_pick_a_stock.py:668-704`
- Test: `tests/test_ak_pick_a_stock.py` near `test_prepare_candidates_uses_historical_factors_to_rank_candidates`

**Interfaces:**
- Consumes: `_rank_bonus(series: pd.Series, weight: float) -> pd.Series`
- Produces: unchanged candidate DataFrame columns and score type; higher `volatility_20d` values produce larger subtracted penalties.

- [ ] **Step 1: Write the failing regression test**

Add the following test after `test_prepare_candidates_uses_historical_factors_to_rank_candidates`:

```python
def test_higher_volatility_receives_larger_score_penalty(monkeypatch):
    factors = {
        "600001": {
            picker.MOMENTUM_20D_COL: 0.10,
            picker.MOMENTUM_60D_COL: 0.20,
            picker.VOLATILITY_20D_COL: 0.01,
            picker.MA_TREND_COL: 0.05,
            picker.AVG_AMOUNT_20D_COL: 500_000_000,
            picker.RELATIVE_STRENGTH_20D_COL: 0.03,
        },
        "600002": {
            picker.MOMENTUM_20D_COL: 0.10,
            picker.MOMENTUM_60D_COL: 0.20,
            picker.VOLATILITY_20D_COL: 0.05,
            picker.MA_TREND_COL: 0.05,
            picker.AVG_AMOUNT_20D_COL: 500_000_000,
            picker.RELATIVE_STRENGTH_20D_COL: 0.03,
        },
    }
    candidates = pd.DataFrame(
        {
            picker.CODE_COL: ["600001", "600002"],
            "score": [100.0, 100.0],
        }
    )

    monkeypatch.setattr(picker, "load_benchmark_history", lambda: pd.DataFrame())
    monkeypatch.setattr(picker, "load_stock_history", lambda code: code)
    monkeypatch.setattr(
        picker,
        "_history_factors",
        lambda code, benchmark_momentum_20d: factors[code],
    )

    result = picker._add_historical_factors(candidates).set_index(picker.CODE_COL)
    ranked = result.sort_values("score", ascending=False)

    assert result.loc["600001", "score"] > result.loc["600002", "score"]
    assert ranked.index[0] == "600001"
```

- [ ] **Step 2: Run the regression test and confirm RED**

Run:

```powershell
rtk pytest -q tests/test_ak_pick_a_stock.py::test_higher_volatility_receives_larger_score_penalty
```

Expected: FAIL at the final assertion because the current reverse ranking subtracts a larger penalty from `600001`, the lower-volatility candidate.

- [ ] **Step 3: Implement the minimal scoring correction**

Replace `_rank_bonus()` with a single-direction helper:

```python
def _rank_bonus(series: pd.Series, weight: float) -> pd.Series:
    numeric = pd.to_numeric(series, errors="coerce")
    ranked = numeric.rank(pct=True)
    return ranked.fillna(0) * weight
```

Change the volatility scoring call to use the same helper without a direction flag:

```python
out["score"] -= _rank_bonus(out[VOLATILITY_20D_COL], 8)
```

Do not change any other factor call, weight, or scoring expression.

- [ ] **Step 4: Run the regression test and confirm GREEN**

Run:

```powershell
rtk pytest -q tests/test_ak_pick_a_stock.py::test_higher_volatility_receives_larger_score_penalty
```

Expected: PASS. `600002` receives the larger volatility penalty, so `600001` retains the higher score.

- [ ] **Step 5: Run the complete picker test file**

Run:

```powershell
rtk pytest -q tests/test_ak_pick_a_stock.py
```

Expected: 17 passed, 0 failed. The baseline contains 16 tests and this task adds one.

- [ ] **Step 6: Run scoped lint verification**

Run:

```powershell
rtk ruff check ak_pick_a_stock.py tests/test_ak_pick_a_stock.py
```

Expected: no Ruff findings.

- [ ] **Step 7: Perform Stage 1 spec-compliance review**

Run:

```powershell
rtk git diff -- ak_pick_a_stock.py tests/test_ak_pick_a_stock.py
```

Verify all of the following before proceeding:

- `_rank_bonus()` has exactly two parameters: `series` and `weight`.
- All five desirable historical factors still add their existing weighted ranks.
- Volatility still uses weight `8` and is subtracted exactly once.
- Missing values still pass through `fillna(0)`.
- The new regression test changes only volatility while holding every other factor equal.
- The new regression test asserts both score direction and final ordering.
- No date handling, filters, output columns, or candidate limits changed.

If any item fails, correct it and rerun Steps 4-6 before Stage 2.

- [ ] **Step 8: Perform Stage 2 code-quality review**

Inspect the same diff and verify:

- no new helper, configuration, dependency, or unrelated formatting was introduced;
- the test name states the behavior rather than the implementation detail;
- the test uses no network access and relies only on deterministic DataFrames;
- the implementation matches the existing pandas style in the file;
- `rtk git diff --check` reports no whitespace errors.

Run:

```powershell
rtk git diff --check
```

Expected: no output and exit code 0.

- [ ] **Step 9: Commit the completed repair**

```powershell
rtk git add ak_pick_a_stock.py tests/test_ak_pick_a_stock.py
rtk git commit -m "fix: correct picker volatility penalty"
```

Expected: one commit containing only the production correction and its regression test.

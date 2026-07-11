# Picker Volatility Ranking Correction Design

## Goal

Correct the A-share candidate picker so higher 20-day volatility receives a
larger score penalty than lower volatility, while leaving every other filter,
factor, weight, and output unchanged.

## Background

`prepare_candidates()` builds an initial score and then calls
`_add_historical_factors()`. Most historical factors are desirable when their
values are higher, so their percentile ranks are added to the score. Volatility
is different: its percentile rank is subtracted.

The current `_rank_bonus()` implementation accepts `lower_is_better=True` and
reverses the rank direction. That gives the lowest volatility the largest
percentile. `_add_historical_factors()` then subtracts that value, causing the
lowest-volatility candidate to receive the largest penalty. This contradicts
the parameter name and the intended risk treatment.

## Scope

This repair will:

- remove the single-use and misleading `lower_is_better` parameter and branch
  from `_rank_bonus()`;
- calculate the volatility percentile in the same ascending direction as the
  other factors;
- continue subtracting the volatility result with the existing weight of 8;
- add a focused regression test proving that, with other inputs equal, a
  higher-volatility candidate receives a larger penalty and ranks below the
  lower-volatility candidate.

## Non-goals

This repair will not:

- change the volatility definition or annualize it;
- change any scoring weights, liquidity thresholds, valuation filters, or the
  top-ten output limit;
- change missing-value behavior (`NaN` continues to contribute zero);
- add scoring configuration or a new ranking abstraction;
- change Beijing-date or market-data behavior.

## Approaches Considered

### Keep `lower_is_better` and change the caller

The caller could add a low-volatility bonus instead of subtracting a penalty.
This can produce the desired ordering, but it preserves a boolean branch used
by only one factor and makes the score harder to audit.

### Keep `lower_is_better` and change its rank direction

The helper could special-case the result so the subsequent subtraction works.
That would retain an unnecessary option whose interaction with addition versus
subtraction remains easy to misuse.

### Use one rank direction and subtract volatility (selected)

All factors use the normal ascending percentile rank. Desirable factors add
their rank; undesirable volatility subtracts its rank. The sign at the call
site expresses the scoring intent directly and removes the confusing branch.

## Data Flow

1. Candidate histories produce `volatility_20d` as the standard deviation of
   recent daily returns.
2. `_rank_bonus(volatility_20d, 8)` assigns larger percentiles to larger
   volatility values.
3. `_add_historical_factors()` subtracts that value from the candidate score.
4. Higher-volatility candidates therefore lose more points before final
   sorting.

Ties continue to receive Pandas' average percentile rank. Missing values
continue to be coerced to `NaN`, filled with zero after ranking, and receive no
penalty.

## Testing

Add a regression test in `tests/test_ak_pick_a_stock.py` that isolates the
volatility contribution. The test will supply two candidates with equal base
scores and equal non-volatility historical factors but different volatility.
It will assert both the score direction and final ordering.

Verification commands:

```powershell
rtk pytest -q tests/test_ak_pick_a_stock.py
rtk ruff check ak_pick_a_stock.py tests/test_ak_pick_a_stock.py
```

The existing picker baseline is 16 passing tests with no Ruff findings in the
two scoped files.

## Compatibility and Risk

The change is internal to the standalone candidate picker. Function output
columns and file formats remain unchanged. Candidate order may change, which is
the intended correction. The regression test prevents the ranking direction
from being inverted again.

# Multi-year price observations in Basic

The market analyst receives a deterministic auxiliary summary from the run's
already bound five-year mainland snapshot, including during structured
finalization. The complete report also contains an independently rendered section
and `1_analysts/multiyear_trend.md`. This adds no data/model calls, SourceFacts,
Strategy Rules, rating premises or changes to the validated decision document.

`tradingagents.multiyear_trend` requires authoritative mainland equity identity,
CNY/qfq, matching symbol/as-of/provider, a valid v2 manifest, and the exact CSV
artifact of an available market-snapshot acquisition. It does not open a cache,
download data, combine providers, or trust model-written trend values. Legacy or
incomplete bindings produce an unavailable section, not guessed numbers.

- Requested window: as-of minus five calendar years, February 29 clamped to
  February 28 if necessary. Actual first/last observation dates and row count are
  displayed; the first close is the cumulative return baseline.
- Cumulative price return: last close / first close - 1. These are forward-adjusted
  closing prices, **not** dividend-reinvested total returns and not annualized.
- Yearly intervals: last observed close of a year / last observed close of the
  immediately preceding year - 1. Without that baseline, use the first observed
  close in the year and explicitly label the interval partial. Show actual
  baseline/end dates and prices. A single endpoint has no return. The cutoff year
  is marked partial whenever as-of precedes December 31.
- Maximum drawdown: max(1 - close / prior observed closing high), including the
  current close in the running high. Show peak/trough dates and prices. Ties use
  the earliest peak and earliest maximum trough. A non-declining series has zero
  drawdown and no fictitious peak/trough pair. This is not intraday drawdown.
- Fraction comparisons are exact; ratios are rounded half-even to eight decimal
  places, percentages displayed to two. The result carries CalculationLineage
  with input hash, snapshot ID, observed range/count, implementation version and
  a reproducible result digest. It is not registered as a decision calculation.

Duplicate, unordered, future or out-of-window dates, row-count/end mismatches,
known gaps, non-finite/nonpositive prices and invalid OHLCV fail closed. No
forward filling or silent sorting/filtering occurs. Input is bounded to 1,831
rows and 2 MB; numeric cells are bounded to 80 characters and exponent magnitude
100. Only observed rows support the statistics. The current EvidenceState does
not carry independently verifiable calendar coverage, so this projection
explicitly leaves five-year/full-year session completeness unverified, even when
the acquisition previously checked a calendar. Missing observations can hide
drawdown extrema. CURRENT/retrospective data does not prove historical
availability or confer strict point-in-time backtest qualification.

Original run reports are never rewritten. An offline preview is a separate
derived document and must state that no model was rerun. See
`tests/test_multiyear_trend.py` for synthetic calculations, binding negatives,
real market-node prompt/finalizer wiring, and report/decision separation.

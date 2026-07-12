# A-Share Local Sentiment Cutoff Safety Design

## Goal

Prevent China A-share local sentiment output from including rows after the
requested analysis window, while keeping unaffected sentiment sources usable
and leaving all non-A-share data paths unchanged.

## Background

`get_china_a_local_sentiment()` labels its output with a requested start and
end date. Popularity and northbound-holdings data pass through
`_filter_date_range()`, but the Eastmoney stock-comment section currently
ignores the requested window and returns the latest matching snapshot.
Historical analysis can therefore receive a stock-comment row from after its
selected cutoff date.

The shared date helper also returns the original frame when its expected date
column is absent. If an AKShare response changes shape, this silently disables
the cutoff for popularity or northbound data instead of degrading that
optional section.

## Date and Timezone Semantics

The requested start and end values are inclusive China A-share calendar dates
in `Asia/Shanghai` (UTC+8). AKShare trading-date fields are interpreted as
Beijing calendar dates. A row is future data when its A-share calendar date is
later than the requested end date.

The comparison must not depend on the host machine's timezone. In particular,
running the tool from a UTC-8 machine must not shift an AKShare date into the
previous or following day. Date-only values are compared as calendar dates,
not converted through UTC instants.

## Selected Approach

Keep the repair inside the existing China A-share local sentiment module:

1. Change `_stock_comment_section()` to accept `start_date` and `end_date`.
2. After selecting the requested stock code, filter the `交易日` field through
   the same inclusive date-window helper used by the other dated sources.
3. Change `_filter_date_range()` to return an empty frame when the expected
   date column is absent. Invalid or unparseable date values likewise do not
   pass the window mask.
4. Pass the requested window from `get_china_a_local_sentiment()` into the
   stock-comment builder.

An empty filtered result omits that optional source section and adds its
existing `DATA_DEGRADED` signal. Other local sentiment sections continue to be
returned normally.

## Alternatives Considered

### Filter only the stock-comment section

This is the smallest code change, but it leaves the existing fail-open behavior
in `_filter_date_range()`. A missing date column could still leak unbounded
popularity or northbound data into a historical request.

### Harden every source in `china_a_enhancements.py` at the same time

Several enhancement sources expose current-only snapshots and deserve a
separate as-of audit. Combining that larger multi-source repair here would
expand the blast radius and make it harder to verify this focused local
sentiment correction.

## Error Handling

Local sentiment sources remain optional:

- an in-window row is formatted exactly as before;
- a matching stock with only out-of-window rows is treated as unavailable for
  the requested window;
- a missing expected date column fails closed instead of returning unfiltered
  records;
- one degraded source does not prevent other valid sections from appearing;
- the output continues to instruct the analyst not to fabricate degraded or
  missing values.

No new exception type, dependency, configuration option, or fallback source is
introduced.

## Testing

Add deterministic tests in `tests/test_china_sentiment.py` that use local
DataFrames and no network access:

- a stock-comment row after the requested Beijing-date cutoff is excluded and
  produces a degraded-source message;
- a frame missing its expected date column cannot bypass
  `_filter_date_range()`;
- the existing in-window formatting test continues to prove that valid local
  sentiment remains present.

Run the focused test file and scoped Ruff checks. The repository baseline is
705 passing tests.

## Scope and Non-goals

This repair changes only:

- `tradingagents/dataflows/china_sentiment.py`;
- `tests/test_china_sentiment.py`.

It does not change other markets, CLI date selection, the candidate picker,
source weights, caching, `china_a_enhancements.py`, or any API outside the
private `_stock_comment_section()` helper.

## Compatibility and Risk

The public `get_china_a_local_sentiment(ticker, start_date, end_date)` contract
is unchanged. Current or historical requests with valid in-window source rows
retain the same output shape. Historical requests may lose a stock-comment,
popularity, or northbound section when the source cannot prove that its data is
inside the requested window; that omission is the intended safety behavior and
is surfaced explicitly as degraded data.

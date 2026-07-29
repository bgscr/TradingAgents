# Tushare 2,000-point requalification

Date: 2026-07-29  
Scope: the seven requested Tushare endpoints, the existing four-symbol cohort,
two live passes, and no production code, provider routing, configuration, or
database changes.

## Executive verdict

The upgrade removed the measured permission barrier. All 56 calls completed
without a typed failure: the 48 statement, indicator, factor, and name calls
returned data, while all eight `suspend_d` calls returned successful empty
frames. Every symbol/endpoint pair reproduced its schema, reporting-period set,
metadata, and full canonical payload exactly across the two passes.

This is not a reason to make Tushare globally first. The measured result supports
capability-specific admission only:

- **Financial statements — `QUALIFIED_WITH_GATES`** for current acquisition.
  Require `comp_type`-specific field normalization, period-level completeness,
  duplicate/revision handling, and `f_ann_date` gating. The raw provider-union
  schema is not itself acceptable.
- **Financial indicators — `QUALIFIED_CURRENT_ONLY`**. The latest five annual
  and eight reporting periods are usable, but `fina_indicator` supplies
  `ann_date` and not `f_ann_date`, `report_type`, `comp_type`, or `update_flag`.
  It cannot independently establish strict point-in-time lineage.
- **Adjustment factors — `QUALIFIED`** as a native dated factor capability.
  This does not qualify a complete Tushare Provider History Bundle and does not
  displace BaoStock as the Strict History Provider.
- **Suspension/status history — `REJECTED`** as an authoritative combined
  capability. Permission is granted, but this cohort produced no positive
  suspension event and `suspend_d` is event-only rather than complete
  per-session traded/suspended status.
- **Name/ST history — `REJECTED` as the combined capability**. `namechange` is
  qualified for dated name history, but none of the four symbols exercised an
  ST transition and the endpoint is not complete per-session ST status.

## Method and cohort

The existing symbols and date bounds were preserved:

- `600895.SH` / `600895.SS`: Shanghai non-financial.
- `601328.SH` / `601328.SS`: bank and exceptional company taxonomy.
- `000651.SZ`: mature Shenzhen non-financial.
- `301589.SZ`: recent-listing Shenzhen non-financial.
- Range: `2021-01-01` through `2026-07-29`; `namechange` uses the endpoint's
  complete per-symbol history.
- Endpoints: `income`, `balancesheet`, `cashflow`, `fina_indicator`,
  `adj_factor`, `suspend_d`, and `namechange`.
- Calls: 7 endpoints x 4 symbols x 2 runs = 56 physical calls. There were no
  retries.

The existing prototype's diagnostic non-sparse rule was retained: at least 25%
of fields and at least three fields populated, with no more than 50% missing in
the accepted row. Because Tushare returns one union schema spanning company
types, results are shown for both the raw union and the symbol-active field set.
The active-field result is evidence for building a declared `comp_type` mapping;
it is not permission to infer a production core schema dynamically.

## Before/after capability matrix

The “before” column is calculated from the two original formal
`tushare-run-*.json` files. The old smoke artifact recorded isolated factor/name
success before the formal calls. The old raw provider error says `1次/分钟`; the
previous narrative report's “1/hour” wording was inaccurate.

| Endpoint | Before: 8 formal calls | After: 8 calls | After data | Qualification |
|---|---|---|---|---|
| `income` | 0/8; 8 `PERMISSION_DENIED` | 8/8 granted; no failure | 17-31 rows; 13-22 distinct report periods | Statements: qualified with gates |
| `balancesheet` | 0/8; 8 `PERMISSION_DENIED` | 8/8 granted; no failure | 16-35 rows; 13-22 periods | Statements: qualified with gates |
| `cashflow` | 0/8; 8 `PERMISSION_DENIED` | 8/8 granted; no failure | 20-33 rows; 13-22 periods | Statements: qualified with gates |
| `fina_indicator` | 0/8; 8 `PERMISSION_DENIED` | 8/8 granted; no failure | 22-32 rows; 20-21 periods | Current indicators: qualified |
| `adj_factor` | 0/8; 8 `RATE_LIMITED` after prior smoke | 8/8 granted; no failure | 595 rows for the recent listing; 1,349 for each other symbol | Native factor: qualified |
| `suspend_d` | 0/8; 8 `PERMISSION_DENIED` | 8/8 granted; no failure | 0 rows for every symbol in both runs | Full status history: rejected |
| `namechange` | 0/8; 8 `RATE_LIMITED` after prior smoke | 8/8 granted; no failure | 1-3 dated rows per symbol | Name qualified; combined name/ST rejected |

## Period and usable-period coverage

“Interim” means `03-31`, `06-30`, or `09-30`. “Latest 8” is the latest eight
distinct reporting periods at quarterly cadence, including year-end reports.
Counts and period sets were identical in both runs.

| Symbol | Statement annual / interim / distinct | Statement active usable annual / latest 8 | Indicator annual / interim / distinct | Indicator usable annual / latest 8 |
|---|---:|---:|---:|---:|
| `600895.SS` | 6 / 16 / 22 | 5 / 8 for all three statements | 5 / 16 / 21 | 5 / 8 |
| `601328.SS` bank | 6 / 16 / 22 | 5 / 8 for all three statements | 5 / 16 / 21 | 5 / 8 |
| `000651.SZ` | 6 / 16 / 22 | 5 / 8 for all three statements | 5 / 16 / 21 | 5 / 8 |
| `301589.SZ` recent listing | 4 / 9 / 13 | 4 / 8 for all three statements | 5 / 15 / 20 | 5 / 8 |

For the three established companies, the statement period set is
`2020-12-31` through `2026-03-31` at quarterly cadence. The latest eight are
`2024-06-30` through `2026-03-31`. The recent-listing statement set begins at
`2022-12-31` and has the same latest eight. A since-listing exception must use
authoritative listing identity; it must not treat every pre-listing filing
returned by the endpoint as an eligible observed period.

The raw union-schema diagnostic rejects all latest income and balance periods
because company-type-inapplicable fields push missingness over 50%. Cash flow
accepts all latest annual periods but only 2-4 of the latest eight reporting
periods under the union schema. The active-field diagnostic accepts the counts
shown above. This is why `comp_type` normalization is a qualification gate.

## Filing metadata and field missingness

For all statement rows and all four symbols:

- `ann_date`: 100% row and distinct-report-period coverage.
- `f_ann_date`: 100% row and distinct-report-period coverage.
- `report_type`: 100% coverage; observed value `1`.
- `comp_type`: 100% coverage; value `1` for non-banks and `2` for the bank.
- `update_flag`: 100% coverage; observed values `0` and `1`.

`update_flag` is a binary provider flag, not a stable revision identifier by
itself. Immutable payload preservation and a versioned provider revision key are
still required before strict replay can distinguish successive corrections.

| Endpoint | Metric fields in union | Non-bank union missingness | Bank union missingness | Non-bank active missingness | Bank active missingness |
|---|---:|---:|---:|---:|---:|
| `income` | 77 | 54.45%-62.87% | 55.89% | 2.56%-7.78% | 5.65% |
| `balancesheet` | 144 | 56.11%-66.23% | 70.14% | 10.95%-14.59% | 0.00% |
| `cashflow` | 89 | 43.55%-57.81% | 55.66% | 21.50%-32.95% | 24.11% |
| `fina_indicator` | provider schema | 1.38%-12.42% | 46.07% | 0.71%-12.42% | 2.37% |

The bank's high union missingness is taxonomy, not an absence of usable data:
its active statement/indicator fields are as complete as or more complete than
the non-bank fields. For example, its balance sheet populates 43 of 144 metric
fields with zero missing cells; the remaining 101 union fields are inapplicable
or never populated. Non-bank balance sheets populate 55-74 active fields.

`fina_indicator` has 100% `ann_date` coverage but no `f_ann_date`,
`report_type`, `comp_type`, or `update_flag` fields. It must inherit first-use
eligibility from a validated filing binding or remain current-only/degraded.

## Name, ST, factor, and suspension behavior

- `namechange` returned complete `ann_date`, `start_date`, `end_date`, `name`,
  and `change_reason` shapes, with 1-3 rows per symbol. The only historical
  transitions in the cohort were ordinary/G-prefix name changes; no row tested
  an ST or `*ST` transition. It therefore qualifies dated name history, not the
  combined name/ST capability.
- `adj_factor` returned native `trade_date` + factor histories for the complete
  requested date range: 1,349 rows for the three established symbols and 595
  for the recent listing. All four payloads reproduced exactly.
- `suspend_d` accepted every call but returned no event rows. An empty event
  query is not evidence of complete per-session trading status, and the cohort
  contains no positive suspension fixture. BaoStock's dated `tradestatus`
  remains authoritative for the combined status capability.

## Request frequency, reproducibility, latency, and failures

The Tushare primary-source [points/frequency table](https://tushare.pro/document/1?doc_id=290)
states that the 2,000+ tier permits 200 calls/minute and 100,000 calls/day per
API. This is a provider-published entitlement ceiling, not the application's
Operator Safety Ceiling.

Each qualification pass reached at most 28 total attempts in a rolling minute
and four attempts per endpoint in a rolling minute. No `RATE_LIMITED` response
occurred. The prototype did not probe until throttling; the observed execution
therefore establishes successful operation at four calls/minute per endpoint,
while the published tier defines the external ceiling. Production must retain a
lower configured ceiling, shared coordination, sequential fallback, and no
retry burst.

All 28 symbol/endpoint comparisons were exact across runs:

- period sets: 28/28;
- schemas: 28/28;
- metadata: 28/28;
- canonical full payloads: 28/28.

| Endpoint | Mean latency | P95 / maximum | Typed failures |
|---|---:|---:|---|
| `income` | 1,368.5 ms | 2,062.0 ms | none (0/8) |
| `balancesheet` | 1,310.1 ms | 3,096.8 ms | none (0/8) |
| `cashflow` | 1,164.4 ms | 2,886.5 ms | none (0/8) |
| `fina_indicator` | 1,458.5 ms | 3,102.0 ms | none (0/8) |
| `adj_factor` | 961.1 ms | 1,986.3 ms | none (0/8) |
| `suspend_d` | 1,515.0 ms | 3,812.8 ms | none (0/8); eight empty successes |
| `namechange` | 1,886.0 ms | 3,806.4 ms | none (0/8) |

## Recommended capability-specific provider order

These orders do not change the Current Analysis Provider Chain and do not imply
global Tushare-first routing.

1. **Full statements:** Tushare -> AKShare-Sina -> Yahoo, only inside the
   statement capability. Accept Tushare first only after `comp_type`-specific
   normalization, latest-period thresholds, `f_ann_date` gating, and immutable
   revision capture pass; otherwise continue fallback.
2. **Financial indicators:** Tushare -> AKShare -> missing BaoStock ratio
   families -> Yahoo profile snapshot. Treat Tushare indicators as current-only
   unless bound to a statement's validated first-publication lineage.
3. **Adjustment factors:** BaoStock -> Tushare -> AKShare -> Yahoo-derived
   factor. BaoStock remains first because factors, raw observations, and status
   form its already accepted single-upstream Provider History Bundle. Yahoo is
   explicitly degraded/derived.
4. **Suspension/status:** BaoStock per-session `tradestatus` remains the only
   qualified authoritative route. Tushare `suspend_d` may be a supplemental
   event lookup after a positive-event fixture is qualified; AKShare/Yahoo
   remain current-only/degraded sources.
5. **Name/ST:** for name events, Tushare -> AKShare. For dated ST state,
   BaoStock remains primary; Tushare cannot enter the ST route until an ST
   transition fixture and effective-date semantics pass twice.

## Artifacts

- `requalify_tushare_2000.py`: one-command scratch runner.
- `outputs/raw/tushare-2000-run-1.json` and `run-2.json`: full live payloads and
  per-call measurements.
- `outputs/tushare-2000-summary.json`: two-run comparisons and observed request
  frequency.
- `TUSHARE_2000_TO_SPEC_AMENDMENT.md`: ready-to-insert `/to-spec` amendment.


# Mainland A-share provider qualification prototype

Date: 2026-07-29  
Scope: disposable live qualification; no production code, configuration, credentials, reports, checkpoints, or databases changed.

## Executive result

Neither requested policy should ship unchanged. Policy B is semantically safer because BaoStock is not a full-statement provider, but both policies begin financial acquisition with Tushare endpoints that the current token cannot execute. The strongest current-account design is a capability- and completeness-driven AKShare/Yahoo statement hybrid, BaoStock for raw market/status/factors and targeted ratio families, and no Tushare financial route until a 2,000-point token can be executed and requalified.

The AKShare 1.18.81 isolated upgrade produced no coverage, schema, sparsity, status, or period improvement over 1.18.73. It was slower in this run and does not justify upgrading the project dependency.

## Test set and measurement

- `600895.SH` -> `600895.SS`: recent Shanghai non-financial.
- `601328.SH` -> `601328.SS`: bank and exceptional company taxonomy.
- `000651.SZ`: mature Shenzhen non-financial.
- `301589.SZ`: recent-listing Shenzhen non-financial with short history.
- Five provider variants, fifteen capabilities, four symbols, two runs: 600 run-level records and 300 two-run summaries.
- Usable statement period definition for the prototype: at least 25% of provider fields populated, at least three populated fields, and no more than 50% missing fields. Production thresholds below are stricter and apply after company-type normalization.

## Provider capability matrix

| Capability | AKShare 1.18.73 | AKShare 1.18.81 | BaoStock 0.9.3 | Current Tushare token | Yahoo 1.5.1 |
|---|---|---|---|---|---|
| Daily OHLCV | 0/4; Eastmoney disconnect in both runs | Same 0/4 | 4/4, raw status fields, 595-1,349 rows | 4/4, 595-1,349 rows | 4/4, 595-1,349 rows |
| Raw and adjusted prices | Raw/qfq history disconnected | Same | 4/4, raw plus qfq | Raw daily works; factors throttled | 4/4 Close/Adj Close |
| Adjustment factors | 4/4 via `stock_zh_a_daily(qfq-factor)`; 7-39 events | Identical | 4/4 native factor history; 6-9 events | One smoke call succeeded; repeated calls rejected at 1/hour | Derived `Adj Close / Close`, not native |
| Suspension/trading status | Current-stop endpoint disconnected | Same | 4/4 dated `tradestatus` and `isST` | `PERMISSION_DENIED` | Current quote metadata only |
| Name/ST/delisting | Name history + current ST, partial | Identical | IPO/out/current status plus dated `isST`, partial | Name endpoints limited to 1/hour | Current profile only |
| Full balance sheets | 4/4 endpoints; sparse for three symbols | Identical | No: selected balance ratios only | `PERMISSION_DENIED` | 4/4; five annual and five quarterly dates maximum |
| Full income statements | 4/4 with five annual/eight quarterly dates | Identical | No: profit metrics only | `PERMISSION_DENIED` | 4/4; four annual and five-to-six quarterly dates |
| Full cash-flow statements | 4/4 with five annual/eight quarterly dates | Identical | No: cash-flow ratios only | `PERMISSION_DENIED` | 4/4; five annual and five quarterly dates maximum |
| Ratios | Eight dates for three symbols; bank payload too sparse | Identical | Six reproducible metric families; high request cost | `PERMISSION_DENIED` | One current profile snapshot |
| Announcement dates | `公告日期` present for all symbols | Identical | `pubDate` present | Financial endpoints denied | Earnings events are not filing dates |
| First-publication dates | Not returned | Not returned | Not returned | Endpoint would expose `f_ann_date`, but untested | Not returned |
| Restatement/update | `更新日期` present; no restatement flag | Identical | No update/restatement ID | Endpoint would expose `update_flag`, but untested | None |
| Unit/currency/scope/type | `币种=CNY`, `类型`, and update timestamp | Identical | Mostly implicit/provider-defined | Would expose `report_type`/`comp_type`; untested | Currency only; no consolidation scope |

All availability outcomes were reproducible over the two formal runs. The Tushare factor/name smoke success could not be repeated because the provider subsequently returned an exact one-call-per-hour limit.

## Per-symbol financial completeness

The denominator 39 is five annual plus eight quarterly periods for each of three statements. BaoStock numbers are metric-proxy periods and must not be interpreted as full statements.

| Symbol | AKShare full-statement usable / 39 | Yahoo usable / returned | BaoStock metric proxies | Ratio result |
|---|---:|---:|---|---|
| 600895.SS | 17/39 | 27/30 | 39/39 proxy periods | AKShare 8/8; all Bao families usable |
| 601328.SS bank | 13/39 | 27/29 | 26/39 proxy periods | AKShare 0/8; Bao operation 0/11 and cash-flow proxy 0/8, growth/DuPont usable |
| 000651.SZ | 35/39 | 27/29 | 39/39 proxy periods | AKShare 8/8; all Bao families usable |
| 301589.SZ recent IPO | 19/39 | 27/30 | 33/33 available proxy periods | AKShare 8/8; Bao families 9/9 since listing |

Important provider differences:

- AKShare returns much wider native statement schemas: 144-147 balance fields, 80-91 income fields, and 68-111 cash-flow fields. Sparsity is company-type dependent.
- Yahoo returns narrower schemas with lower measured sparsity, but no filing announcement, first-publication, consolidation, or restatement lineage.
- The best measured statement result is not one fixed provider: Yahoo wins balance sheets and exceptional sparse cases; AKShare wins several income/cash-flow histories and supplies filing metadata.

## Current account and entitlement limitations

Tushare token presence was detected without printing or persisting its value.

- `daily`: executed successfully for all four symbols in both formal runs.
- `balancesheet`, `income`, `cashflow`, `fina_indicator`, and `suspend_d`: exact typed result was `PERMISSION_DENIED`, with provider text `抱歉，您没有接口(<endpoint>)访问权限，权限的具体详情访问：https://tushare.pro/document/1?doc_id=108。`
- The API error does not state a numeric point balance. Tushare documentation specifies a 2,000-point baseline for single-stock statement/indicator history and suspension access.
- `adj_factor` and `namechange`: one smoke call succeeded, then repeated calls returned `RATE_LIMITED` with `频率超限(1次/小时)`.
- Therefore the current token is not operationally suitable for analysis-time financial, status, factor, or name-history routing.

BaoStock required no paid credential. Anonymous `bs.login()` and all tested market and financial-metric functions succeeded. BaoStock has no Tushare-style points upgrade.

## AKShare old-versus-new

| Measurement across 60 provider/symbol/capability comparisons | Result |
|---|---:|
| Status differences | 0 |
| Period-set differences | 0 |
| Field-count differences | 0 |
| Missing-ratio differences | 0 |
| Mean recorded latency, 1.18.73 | 5,080.6 ms |
| Mean recorded latency, 1.18.81 | 5,686.1 ms |

Both versions reproduced the same Eastmoney disconnect for daily OHLCV/status and the same successful Sina statements, ratios, announcement metadata, name history, and factor results. No upgrade benefit was measured.

## BaoStock adapter opportunity

BaoStock is qualified for raw daily history, forward factors, per-session `tradestatus`, per-session `isST`, session calendars, and current IPO/out/status metadata. The existing production `load_snapshot_with_history` already models most of this capability, but shadow mode does not use it as the decision source.

Financial functions require one remote request per symbol/year/quarter; the SDK has no bulk-history parameter. For one 11-period symbol qualification, six families require 66 physical requests.

| Family | Fields | Typical periods | Result |
|---|---:|---:|---|
| Profit | 8 | 8 retained | Reproducible; bank somewhat sparse |
| Operation | 6 | 11, or 9 for recent IPO | Reproducible; bank 83.3% missing and unusable |
| Growth | 5 | 11, or 9 | Fully populated and reproducible |
| Balance ratios | 6 | 8 retained | Reproducible; bank 50% missing |
| Cash-flow ratios | 7 | 8 retained | Reproducible; bank 57.1% missing and unusable |
| DuPont | 8 | 11, or 9 | Reproducible; bank 25% missing |

These are valuable ratio adapters, not substitutes for native balance, income, or cash-flow statements.

## Policy scores

Scores are 0-100; request counts are deduplicated measured physical request identities for one four-symbol execution. Point-in-time quality requires both announcement and first-publication dates.

| Policy | Total | Financial coverage | PIT quality | Reliability | Requests | Entitlement | Normalization | Compatibility | Maintenance |
|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|
| A | 47.7 | 57.4 | 0.0 | 100.0 | 373 | 0.0 | 50.4 | 66.4 | 60.4 |
| B | 47.7 | 57.4 | 0.0 | 100.0 | 365 | 0.0 | 50.4 | 66.4 | 60.4 |

Policy B is preferred only narrowly: it does not put BaoStock metric proxies ahead of Yahoo full statements and uses eight fewer measured requests. Neither policy qualifies because Tushare financial primaries are denied and neither policy uses AKShare's actually working statement endpoints.

## Tushare 2,000-point value assessment

Potential value is high but unmeasured with this account. A 2,000-point token should expose the exact missing semantic fields—full statements, `ann_date`, `f_ann_date`, `report_type`, `comp_type`, `update_flag`, indicators, and suspensions—but documentation is not execution evidence. No purchase recommendation is justified until those endpoints can be run on the same four-symbol/two-pass matrix, especially the bank and recent IPO.

## Recommended capability-specific routing

1. Keep the current market-snapshot order unchanged during implementation qualification: AKShare -> BaoStock -> Yahoo.
2. Route raw observations, factors, per-session trading status, and historical ST primarily to BaoStock's history bundle. AKShare factors are a fallback; Yahoo factors are explicitly degraded/derived.
3. Acquire full statements through an AKShare-Sina/Yahoo hybrid:
   - use AKShare for native breadth and `公告日期`/CNY/consolidation/update metadata;
   - validate each normalized period;
   - merge or fall back to Yahoo when AKShare misses the threshold;
   - prefer Yahoo balance sheets generally, and Yahoo balance/cash-flow for banks;
   - prefer AKShare income/cash-flow where it supplies five annual/eight usable quarterly periods.
4. Treat BaoStock only as a ratio-family provider. Use AKShare indicators first for non-banks; call only missing BaoStock families. For banks, skip Bao operation and cash-flow ratios when they fail sparsity thresholds.
5. Use AKShare name changes plus BaoStock dated `isST`, trade status, IPO/out dates. Mark the result degraded because no current provider supplies complete announcement/effective-date event lineage.
6. Do not place Tushare in analysis-time financial routing with the current token. If entitlement changes, rerun this prototype and enable individual capabilities only after they pass.
7. No current provider qualifies for first-publication/restatement lineage. Fundamental point-in-time evidence must remain degraded or insufficient rather than silently substituting announcement/update dates.

## Production adapters required

- Replace AKShare placeholders for `get_balance_sheet`, `get_income_statement`, and `get_cashflow` with `stock_financial_report_sina` adapters, including frequency filtering, company-type normalization, and extraction of `报告日`, `公告日期`, `币种`, `类型`, and `更新日期`.
- Add a normalized AKShare financial-indicator adapter around `stock_financial_analysis_indicator` with bank-specific sparsity rules.
- Expose BaoStock `load_snapshot_with_history` as explicit raw-price, factor, trading-status, and ST capabilities rather than relying on the current adjusted-frame path.
- Add separate BaoStock ratio-family adapters for `query_profit_data`, `query_operation_data`, `query_growth_data`, `query_balance_data`, `query_cash_flow_data`, and `query_dupont_data`; cache by symbol/year/quarter and never advertise them as statements.
- Add a capability contract to the financial dispatcher so `CAPABILITY_PARTIAL_METRICS_NOT_FULL_STATEMENT` continues fallback.
- Add completeness-based AKShare/Yahoo statement merge/fallback and retain both provider manifests.
- Add Tushare adapters only behind an entitlement probe for `balancesheet`, `income`, `cashflow`, `fina_indicator`, `suspend_d`, `adj_factor`, `namechange`, and `stock_basic`; keep disabled until live qualification passes.
- Extend evidence projection with dataset/period coverage, original announcement, first-publication, update/restatement, unit, currency, consolidation, and company-type metadata.

## Proposed production completeness thresholds

- OHLCV: at least 99.5% of expected exchange sessions, no missing OHLCV, no duplicate dates, valid OHLC ordering, and no staleness beyond one session.
- Raw/adjusted: both raw and adjusted series plus a dated factor whose first effective date covers the first retained observation.
- Status: explicit traded/suspended state for every open exchange session; unknown status cannot be institutional-grade.
- Statements: five usable annual and eight usable quarterly periods for each of the three statements. A recent IPO may use all periods since listing, but the exception must be explicit.
- Normalized statement core: 100% of critical fields and at least 90% of the company-type-specific core field set per period. Provider-wide union-schema sparsity is diagnostic, not the normalized threshold.
- Ratios: eight quarterly periods, documented formula/unit, and no more than 10% missing normalized fields in any accepted period.
- Point-in-time: announcement date and first-publication date required for every accepted period; no data may be used before first publication.
- Restatement: stable filing/update identifier required; value changes without a new identifier quarantine the dataset.
- Metadata: unit, currency, consolidation scope, and company type required on every accepted statement.
- Reproducibility: two runs must return the same period set, schema, and metadata identifiers; value changes require explicit provider revision lineage.
- Capability semantics: selected metrics or ratios can never satisfy a full-statement capability.

## Disposable artifacts

- `outputs/raw_records.json`: all 600 run-level records.
- `outputs/summary_records.json`: 300 two-run provider/capability/symbol summaries.
- `outputs/provider_capability_matrix.csv`: aggregated provider matrix.
- `outputs/per_symbol_completeness_matrix.csv`: complete per-symbol detail.
- `outputs/baostock_financial_api_details.json`: per-family operation/growth/DuPont breakdown.
- `outputs/policy_scores.json`: Policy A/B selections and scores.


# Multi-Horizon A-Share Picker and PIT Evaluation Design

Date: 2026-07-12
Branch: `codex/multi-horizon-picker-design`
Status: Approved design

## Goal

Turn the standalone A-share picker into a measurable multi-horizon research
system. The system will produce separate 5-trading-day, 20-trading-day, and
consensus candidate lists, while a point-in-time (PIT) walk-forward evaluator
determines which factors and parameters are allowed into validated rankings.

The design combines two initiatives that must reinforce each other:

1. improve daily A-share candidate selection; and
2. prove improvements out of sample under realistic China-market execution
   constraints.

The picker remains a research tool and does not place live orders or present
its output as investment advice.

## Current State and Motivation

The current `ak_pick_a_stock.py` is a single-file live picker. It:

- fetches current full-market spot data from Eastmoney/AKShare fallbacks;
- excludes ST names, prices at or below RMB 3, and daily traded amount at or
  below RMB 300 million;
- applies current valuation filters when valuation data exists;
- scores liquidity, daily change, turnover, 20/60-day momentum, relative
  strength, moving-average trend, average amount, and volatility; and
- writes one top-ten CSV.

The implementation has several limits relevant to the next expansion:

- spot fields are not a historical PIT universe;
- historical normalization retains only date, close, and amount;
- there is no portfolio/execution state, corporate-action ledger, or
  walk-forward evaluator;
- turnover receives a binary reward in a narrow range but no explicit extreme
  turnover penalty;
- rankings are not neutralized for size or constrained for concentration; and
- factor changes cannot be evaluated against a consistent out-of-sample
  baseline.

## Scope

This design covers:

- a hybrid live/PIT data engine;
- reproducible historical data caching;
- PIT universe reconstruction and eligibility;
- reusable factor computation;
- independent 5-day and 20-day rankers;
- a penalized rank-aggregation consensus list;
- size neutralization and list-concentration controls;
- realistic A-share execution, including T+1, suspensions, price limits, and
  forced limit-down holds;
- corporate-action accounting for unadjusted execution prices;
- purged walk-forward evaluation, ablation, and promotion gates;
- three live ranking outputs plus backward compatibility; and
- phased delivery with explicit contracts.

## Non-goals

- Do not change the TradingAgents LangGraph shape.
- Do not make Tushare mandatory for the existing live keyless picker.
- Do not silently approximate PIT-critical fields from current snapshots.
- Do not add machine-learning model training in the first delivery. Initial
  horizon profiles use transparent factor weights and bounded parameter grids.
- Do not place broker orders or claim production trading readiness.
- Do not bundle or redistribute Tushare data in portable releases.
- Do not implement multiple commercial PIT providers in the first delivery.

## Architecture

Keep `ak_pick_a_stock.py` as the compatible CLI and portable entry point. Move
the expanding domain logic into a focused `tradingagents/picker/` package:

- `models.py`: typed snapshot, factor, ranking, portfolio, execution, and
  corporate-action records;
- `providers.py`: the historical snapshot provider contract and Tushare
  implementation boundary;
- `cache.py`: immutable raw and normalized dataset partitions;
- `snapshot.py`: PIT universe and feature-input construction;
- `features.py`: factor definitions, nonlinear mappings, normalization, and
  contribution metadata;
- `ranking.py`: horizon scoring, diversification, and consensus aggregation;
- `execution.py`: orders, fills, T+1 state, price-limit locks, suspensions,
  transaction costs, and portfolio accounting;
- `corporate_actions.py`: entitlements, receivables, share credits, and
  reconciliation;
- `evaluation.py`: walk-forward folds, cohorts, metrics, ablations, and
  promotion gates; and
- `reporting.py`: live CSV/metadata output and immutable evaluation reports.

Each unit has one public purpose and can be tested from cached fixtures without
network calls. The live picker uses the same feature and ranking code as the
evaluator; only the snapshot provider differs.

## Hybrid Data Engine

### Live keyless path

AKShare/Eastmoney remains the primary current-market source. BaoStock remains a
keyless historical-bar fallback where its contract is sufficient. These
sources support live research but are not used to fabricate a validated
historical cross-section.

### Tushare PIT path

Tushare Pro is an optional dependency and uses `TUSHARE_TOKEN`. It is optional
for live ranking but required for a run labeled `validated backtest`. Missing
credentials or endpoint permissions disable validated backtesting without
breaking the live keyless picker.

The initial `HistoricalSnapshotProvider` has one implementation: Tushare. The
interface permits later replacement, but no speculative second adapter is
built now.

The PIT dataset combines:

- `trade_cal` for the exchange calendar;
- `stock_basic` queried across listed, delisted, and paused/list-status records
  for listing and delisting dates;
- `daily` for unadjusted OHLCV execution data;
- `daily_basic` for daily turnover, share counts, `circ_mv`, and historical
  valuation fields;
- `moneyflow` for date-bounded stock-level fund-flow inputs;
- `adj_factor` for adjusted factor histories and reconciliation;
- `namechange` for effective historical ST/name intervals;
- `suspend_d` for suspensions;
- date-specific upper/lower price limits for execution restrictions;
- `dividend` or the provider's explicit implemented-action feed for cash and
  stock distributions; and
- historical first-level industry membership with effective in/out dates when
  the account's endpoint permissions provide it.

`namechange` is a separate dataset, not a field of `daily_basic`. Current-only
industry labels are never treated as PIT industry membership.

### Canonical market-cap fields

Tushare's normalized contract distinguishes circulating market value from
true free-float market value. `daily_basic` uses `circ_mv`; `float_mv` is not a
canonical field in this design.

The provider adapter converts Tushare's documented ten-thousand-unit fields to
CNY and exposes both values:

```text
circ_market_cap_cny = circ_mv * 10_000
free_float_market_cap_cny = free_share * close * 10_000
```

`free_share` is measured in ten thousand shares and `close` in CNY per share.
The RMB 500 million eligibility floor, size buckets, neutralization, and risk
controls all consume only `free_float_market_cap_cny`. Raw `circ_mv`,
`free_share`, and `float_share` remain provenance fields and cannot be read
directly by Phase 2 or Phase 3 domain logic.

Normalization fails a symbol/date when required values are missing, units are
not the declared units, `free_share > float_share`, `float_share > total_share`,
or `circ_market_cap_cny` does not reconcile with
`float_share * close * 10_000` within the provider rounding tolerance.
Reconciliation permits the larger of RMB 10,000 or 0.1% of reported
circulating market value. This prevents different phases from silently using
different definitions of market capitalization.

### Cache and provenance

Ingestion queries full-market data by trading date, not stock by stock. It
writes immutable raw responses and normalized Parquet partitions. Every
partition records:

- provider and endpoint;
- requested and effective trading date;
- fetch timestamp;
- schema version;
- row count;
- content checksum; and
- required-field coverage.

Backtests read only cached partitions. Re-fetching creates a new version rather
than mutating a prior run's evidence. A run manifest pins the exact partition
checksums used.

### Rate limiting and resume

Historical ingestion is paced by an endpoint-aware token-bucket limiter and
uses one network worker by default. Tushare does not provide one dependable
quota-discovery contract for every token and endpoint, so the implementation
must not claim automatic tier detection when it cannot prove it. The effective
calls-per-minute value comes from an explicit endpoint override or
`TUSHARE_CALLS_PER_MINUTE`; absent either, the conservative default is 40 calls
per minute.

On a throttling response, ingestion honors a provider retry delay when one is
available. Otherwise it uses exponential backoff with jitter and reduces its
effective rate for the rest of the run. After eight failed attempts, it records
the partition as pending and exits non-zero rather than spinning indefinitely.

Each successfully validated partition is written atomically before the next
request and immediately recorded in the checkpoint manifest. A resumed run
skips checksum-verified partitions, retries pending/failed partitions, and
never discards completed dates. If an endpoint hits its maximum row count, the
adapter must paginate and reconcile the combined row count before marking the
partition complete.

Tushare materially improves PIT reconstruction but is not assumed infallible.
Provider revisions, missing coverage, permissions, and publication/effective
dates remain explicit validation concerns. A value is eligible only when its
effective or publication timestamp is no later than the snapshot cutoff.

## PIT Universe and Eligibility

A signal snapshot is formed after the close of trading day `T`. Eligibility
uses only information effective by that cutoff. The historical universe
includes securities that later delisted so future survival is not an implicit
filter.

A candidate must satisfy all of the following:

- at least 60 completed trading days since listing;
- not ST or under another effective special-treatment name interval;
- closing price above RMB 3;
- free-float market capitalization of at least RMB 500 million;
- trailing liquidity above the existing RMB 300 million amount floor, measured
  without future spot data;
- tradable and not suspended on signal day `T`; and
- complete critical price, tradability, market-cap, and risk fields.

The RMB 3 price rule is intentionally stricter than a RMB 1.2 par-value
delisting early-warning line. The RMB 500 million free-float floor is a
conservative universal minimum across boards; future rule tables may raise but
must not silently lower it for a validated configuration.

Missing optional alpha fields do not make a candidate eligible for extra
weight. They receive a neutral factor value of 50, and the finished horizon
score loses `10 * missing_optional_factor_fraction` points. Missing critical
risk or execution fields make the candidate ineligible. Suspension or another
execution lock first observed on `T+1` is handled as an unfilled order, not an
eligibility exclusion known on `T`.

## Factor Engine

The feature engine computes one reusable factor matrix per date. Each factor
declares:

- source fields and PIT availability rule;
- lookback window;
- favorable direction or bounded-optimum mapping;
- missing-data policy;
- critical/optional classification; and
- contribution to each horizon profile.

Raw continuous factors are winsorized cross-sectionally before ranking. For
ordinary monotonic factors, scores are percentiles from 0 to 100. Risk factors
reverse direction at the factor definition, so call sites do not combine an
ambiguous rank direction with a separate sign.

### Nonlinear turnover and volume

Turnover is explicitly non-monotonic. The initial transparent trapezoid is:

- at or below 1%: score 0;
- 1% to 5%: linearly rise from 0 to 100;
- 5% to 15%: score 100;
- 15% to 30%: linearly fall from 100 to 0; and
- at or above 30%: score 0.

Volume surprise uses a bounded-optimum mapping rather than an unlimited
higher-is-better percentile: relative volume below 0.5 or above 4.0 scores 0,
1.2 through 2.5 scores 100, and the intervening shoulders interpolate
linearly. These are initial experimental mappings, recorded in run metadata
and eligible for promotion only through the predefined walk-forward process.

### Horizon profiles

The 5-day profile emphasizes:

- 5/10-day momentum;
- momentum acceleration;
- bounded volume surprise;
- bounded turnover quality;
- date-proven recent fund-flow change;
- date-proven sentiment;
- short relative strength;
- overextension risk; and
- 20-day volatility.

The 20-day profile emphasizes:

- 20/60-day momentum;
- moving-average trend and persistence;
- medium relative strength;
- average liquidity;
- 60-day drawdown;
- volatility; and
- PIT valuation when reconstructible.

Live-only context without defensible historical timestamps may be displayed
but cannot affect a ranking labeled validated.

A factor whose dataset is unavailable for the entire run is disabled in the
versioned configuration and does not dilute every candidate's score. The
neutral-value and confidence-penalty rule applies when an enabled optional
factor is missing for an individual candidate.

Every factor maps to 0 for worst and 100 for best, including risk factors whose
raw direction is reversed. Until walk-forward promotion selects another
version, a horizon score is the equal-weight mean of its declared factors,
minus the missing-data penalty above. The score is then converted to a
cross-sectional percentile for ranking. The complete versioned configuration
is saved with every run; hand-edited production weights are not permitted.

## Size Neutralization and Concentration

On every date, eligible candidates are divided into free-float market-cap
quintiles. Monotonic factor percentiles are computed within the candidate's
size quintile, preventing raw small-cap elasticity or large-cap defensiveness
from dominating the market-wide score.

Ranking output preserves both the unconstrained raw rank and the selected
rank. The selector scans raw order and admits at most:

- three names from one market-cap quintile; and
- three names from one PIT first-level industry.

If PIT industry membership is unavailable, the industry concentration control
is disabled with an explicit run-level warning. Size neutralization and the
size cap remain mandatory. Unknown industry is one bucket and is subject to the
same cap when industry controls are active.

## Three Rankings and Conflict Control

The system emits three separate lists:

1. top 5-day candidates ranked by the 5-day profile;
2. top 20-day candidates ranked by the 20-day profile; and
3. a consensus list derived from the two finished horizon rankings.

The consensus never merges raw factors. Let `p5` and `p20` be each candidate's
0-100 percentile within the eligible universe. The initial consensus is:

```text
consensus_score = 0.5 * p5 + 0.5 * p20 - lambda * abs(p5 - p20)
```

The initial `lambda` is 0.25. The only comparison grid is `{0.15, 0.25, 0.35}`;
adding values requires a new design decision. A consensus candidate must be at
or above the 50th percentile on both horizons. The list returns up to ten names
and is never padded with candidates that fail either floor.

Every consensus row exposes `p5`, `p20`, raw disagreement, penalty, pre- and
post-constraint rank, factor confidence, and rejection/degradation metadata.

## Signal Timing and Execution

Signals use data available after close on `T`. Orders execute no earlier than
the next trading session `T+1`. A closing price cannot be both an input and an
execution price for the same signal.

The simulator maintains settled cash, positions, T+1-restricted lots, pending
orders, pending exits, corporate-action receivables, and non-tradable share
entitlements.

Execution rules are:

- shares bought today cannot be sold until the next trading day;
- suspension prevents entry and exit and forces an existing position to stay;
- a one-word limit-up makes a buy unfilled and cancelled for that rebalance;
- a one-word limit-down leaves a sell pending and forces the position to stay;
- cash from a blocked exit remains unavailable;
- the first unlocked session fills a pending exit at that session's open plus
  sell-side slippage; and
- a confirmed delisting without a tradable terminal price is marked to zero
  and reported as a terminal-loss event.

A one-word lock requires the unadjusted open, high, low, and close to equal the
provider's applicable limit price within tick-size tolerance. If the daily
range proves the limit opened, the session is not classified as one-word
locked; the normal fill rule applies.

Price-limit detection uses the provider's date-specific limit prices and
unadjusted OHLC. It does not infer every board and historical regime from a
hardcoded percentage. Features use adjusted histories, while orders, fills,
and price-limit state use unadjusted prices.

Transaction costs come from a versioned effective-date schedule covering
commissions, sell-side stamp duty, transfer charges, and configurable liquidity
slippage. Every fill records the applied schedule version and cost components.

## Corporate-Action Ledger

Unadjusted execution requires explicit economic entitlements. The simulator
uses implemented corporate-action records as the source of truth and treats
`adj_factor` as a reconciliation signal, not a direct share-count instruction.
An adjustment-factor ratio can reflect cash dividends as well as stock changes
and therefore must not be multiplied blindly into shares.

Before each market session the ledger processes:

1. record-date holdings to establish entitlement;
2. ex-date stock/cash receivables so NAV does not suffer a phantom loss;
3. bonus/transfer share listing dates to move pending shares into tradable
   inventory; and
4. cash payment dates to move dividend receivables into settled cash and
   buying power.

Rights issues, mergers, and other actions use distinct event types. Rights
issues are deliberately unsupported in the first implementation: the ledger
must never create cash or shares for them and must never freeze subscription
cash automatically. A symbol with a known active subscription window is
ineligible for a new position. If a rights issue becomes effective while a
symbol is held, the affected validated portfolio and fold stop with an
`UNSUPPORTED_RIGHTS_ISSUE` verdict. The backtester must not remove the symbol
and continue, because that would introduce post-selection survivorship bias.
Live or paper mode emits a blocking manual-action warning.

Cash receivables contribute to NAV from ex-date but not buying power before
payment. Pending shares contribute to economic NAV but cannot be sold before
their listing date. Fractional entitlements follow a versioned rounding or
cash-in-lieu rule. The initial validated rule rounds tradable shares down to a
whole share and credits cash-in-lieu only when the provider supplies an
implemented cash value; an unresolved remainder fails reconciliation.

The ledger's unadjusted-price total return must reconcile with the provider's
adjusted return within five basis points per event. A mismatch is a
data-quality failure and never causes automatic share mutation.

At every session:

```text
NAV = settled cash + cash receivables + market value of tradable shares
      + economic value of pending share entitlements
```

## Walk-Forward Evaluation

Use at least five years of data spanning large/small-cap, rising, falling, and
sideways regimes. The default fold is:

- rolling 24-month training window;
- six-month validation window;
- 20-trading-day embargo; and
- six-month untouched test window.

The embargo prevents overlapping 20-day labels from leaking between windows.
Factor weights, nonlinear mapping variants, and `lambda` may be selected only
on training and validation data. Each test window is evaluated once.

The current picker is reconstructed under the same PIT universe, costs, and
execution rules to form the baseline. New factors are evaluated individually
and with leave-one-factor-out ablation.

Evaluation has two levels:

1. daily cross-sectional cohorts measure rank IC, hit rate, downside tails,
   and 5/20-day forward excess return; and
2. executable staggered-cohort portfolios reduce arbitrary start-date effects.

Four executable books remain separate:

- 5-day list held/evaluated at five trading days;
- 20-day list held/evaluated at 20 trading days;
- consensus list evaluated at five trading days; and
- consensus list evaluated at 20 trading days.

The default broad-market excess-return benchmark is CSI All Share; CSI 300 is
reported secondarily for compatibility with the existing picker. Benchmark
symbols and partitions are pinned in run metadata.

Primary metrics include:

- cross-sectional rank IC and IC stability;
- top-ten excess return and hit rate;
- net return after costs;
- maximum drawdown, downside-tail return, and cash drag;
- turnover and ranking stability;
- forced-hold frequency and loss from suspensions/limit-down locks;
- corporate-action reconciliation failures;
- market-cap and industry exposure; and
- sensitivity to plus/minus 10% factor-weight perturbations.

## Promotion Gates

A factor or parameter change is promoted only when it:

- improves median out-of-sample target-horizon excess return after costs;
- produces positive rank IC in a majority of test folds and represented market
  regimes;
- does not worsen maximum drawdown by more than the larger of two percentage
  points or 10% of the baseline drawdown magnitude;
- does not worsen locked-position exposure by more than the larger of 0.5
  percentage points or 10% of the baseline rate;
- retains required data coverage;
- survives weight-perturbation testing; and
- does not depend on one exceptional fold, industry, or size bucket.

The 5-day list is promoted on 5-day evidence and the 20-day list on 20-day
evidence. Consensus must improve or preserve both horizon results; it cannot
trade away one horizon to improve a blended average. Every promotion creates a
new immutable ranking-configuration version and includes its ablation report.

## Outputs and Compatibility

Live runs write:

- `ak_candidates_5d.csv`;
- `ak_candidates_20d.csv`;
- `ak_candidates_consensus.csv`;
- `ak_candidates.csv`, retained as a compatibility copy of consensus; and
- `ak_candidates_metadata.json`.

When `AK_PICK_OUTPUT_PATH` names the legacy CSV, that path receives consensus
and the horizon-specific files are siblings using the same base directory.
Portable release paths remain under the release's `reports` directory.

Rows include symbol/name, as-of date, raw and selected ranks, relevant horizon
scores, consensus components, factor contributions, confidence, size bucket,
industry, provider provenance, and warnings.

Validated backtests create an immutable run directory containing:

- configuration and data manifest;
- fold definitions and metrics;
- candidate/rejection records;
- orders, fills, costs, and forced-hold periods;
- corporate-action events and reconciliation results;
- coverage failures;
- ablation and promotion verdicts; and
- a human-readable Markdown summary.

## Error Handling

- Missing Tushare credentials disables validated backtesting only.
- Missing endpoint permissions fail a startup capability check with the exact
  required dataset named.
- Missing mandatory PIT partitions invalidate the affected evaluation date; no
  keyless fallback fills them.
- Exhausted ingestion retries leave the partition pending and the command exits
  non-zero; a subsequent run resumes from the checkpoint manifest.
- Missing PIT industry history activates documented size-only mode.
- Missing execution, market-cap, or corporate-action data fails the symbol/date
  closed.
- A date is covered when at least 95% of its reconstructed active securities
  have either the mandatory raw fields or an explicit suspension/status record.
  A fold requires at least 95% of its scheduled signal dates to be covered;
  otherwise the fold fails instead of reporting a biased metric.
- Network retries occur only during ingestion. Evaluation never reaches the
  network.
- `TUSHARE_TOKEN` is never logged, written to metadata, or embedded in a
  portable build.

## Testing

Unit tests cover:

- nonlinear factor mappings and boundary values;
- winsorization, size-neutral percentiles, and missing-data confidence;
- horizon scores and factor contributions;
- consensus floors, penalty grid, and list-size behavior;
- size/industry caps and raw-versus-selected ranks;
- PIT availability and effective-date comparisons;
- cache checksums and manifest pinning;
- token-bucket pacing, adaptive throttling, retry exhaustion, atomic partition
  writes, pagination, and checkpoint resume using a fake clock/provider;
- canonical `circ_market_cap_cny` and `free_float_market_cap_cny` conversion,
  unit validation, and share-count reconciliation;
- T+1 lots, suspensions, price-limit entry/exit behavior, and costs;
- consecutive one-word limit-down forced holds;
- cash dividends, bonus shares, pending entitlements, and payment/listing dates;
- rights-issue entry exclusion and fail-closed held-position behavior;
- adjustment-factor reconciliation failures; and
- accounting invariants for cash, receivables, positions, and NAV.

Integration tests use checked-in or generated cached fixtures and make no live
API calls by default. Optional live Tushare tests have an explicit integration
marker and require credentials. Golden-output tests preserve the legacy
`ak_candidates.csv` path and portable entrypoint behavior.

Walk-forward tests use small deterministic synthetic folds to verify embargoes,
single-use test windows, ablation isolation, promotion gates, and reproducible
reports.

## Phased Delivery

This is an umbrella architecture, not one oversized implementation change.
Delivery is gated:

### Phase 1: PIT data foundation

Implement Tushare capability checks, paced/resumable ingestion, immutable
cache, manifests, canonical market-cap fields, and the PIT snapshot/universe
contract. Verify reconstructible delisted/ST fixtures, throttle recovery,
checkpoint resume, and fail-closed coverage.

### Phase 2: Multi-horizon ranking

Implement the feature engine, nonlinear mappings, size controls, two horizon
rankers, penalized consensus, three outputs, and legacy output compatibility.
The lists remain explicitly experimental until Phase 4 validation.

### Phase 3: Execution realism

Implement portfolio state, T+1, suspensions, price-limit locks, effective-date
costs, corporate-action ledger, and accounting reconciliation.

### Phase 4: Evaluation and promotion

Implement purged walk-forward folds, staggered portfolios, metrics, ablation,
parameter selection, and immutable promotion reports.

Each phase receives a scoped implementation plan and must pass its contract and
regression tests before the next phase begins. A phase may refine internal
details but cannot weaken PIT, fail-closed, accounting, or promotion contracts
without a reviewed design amendment.

## Acceptance Criteria

The program is complete when:

- the live picker emits three explainable lists and preserves the legacy CSV;
- validated backtests use only manifest-pinned PIT data;
- delisted, ST, suspended, limit-locked, and corporate-action cases are modeled
  without look-ahead or phantom returns;
- factor and consensus changes receive reproducible out-of-sample verdicts;
- consensus disagreement remains visible and is penalized consistently;
- size and concentration controls prevent one style or industry from
  monopolizing a list;
- accounting invariants hold for every simulated session; and
- no ranking configuration is labeled validated without passing its promotion
  gates.

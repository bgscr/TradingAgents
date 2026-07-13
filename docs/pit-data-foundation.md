# Point-in-time data foundation

The Phase 1 point-in-time (PIT) data foundation builds a local, reproducible
China A-share universe from Tushare bulk endpoints. It is optional and is not
required for the normal TradingAgents installation or interactive CLI.

## Install and configure

Install the optional provider and Parquet dependencies:

```bash
pip install ".[pit]"
```

Set `TUSHARE_TOKEN` to a token with access to the six Phase 1 endpoints. The
token is read from the environment and is never printed by the CLI.

Request pacing defaults to 40 calls per minute. The precedence, from highest
to lowest, is:

1. An endpoint-specific variable such as
   `TUSHARE_DAILY_BASIC_CALLS_PER_MINUTE`.
2. The CLI-wide `--calls-per-minute` option.
3. `TUSHARE_CALLS_PER_MINUTE`.
4. The default of 40 calls per minute.

Each endpoint has its own token-bucket limiter. A throttled request is retried
up to eight attempts with backoff; repeated throttling also reduces that
endpoint's rate for the current process.

## Backfill and inspect a snapshot

Backfill an inclusive requested date range:

```bash
tradingagents-pit backfill --start-date 20210101 --end-date 20260710
```

Use `--cache-dir PATH` to override the PIT cache location and `--refresh` to
fetch every planned partition again even when a verified complete version is
already cached. Without `--refresh`, complete partitions are checksum-verified
and skipped. The command reports completed, skipped, and failed partition
counts together with the run ID.

Build a machine-readable snapshot from verified local data:

```bash
tradingagents-pit snapshot --date 20260710
```

The sorted JSON output includes the requested `as_of` date, active and eligible
row counts, coverage, and warnings. Snapshot construction does not call the
provider.

## Date ranges and resuming

Every backfill automatically extends the requested start date by 120 calendar
days. For the example above, the run manifest records:

- requested range: `20210101` through `20260710`;
- effective range: `20200903` through `20260710`.

The warm-up supplies the prior open sessions needed by trailing liquidity and
listing-age calculations. It changes only the effective ingestion range; the
requested range remains recorded separately.

Partition state and the current per-run manifest are updated after each
partition. If the process is interrupted, the run is finalized as
`interrupted`; if throttling exhausts all retries, it is finalized as `failed`.
In both cases, rerun the same command. Verified complete partitions are skipped
and pending or failed partitions are retried, so the backfill resumes without
re-downloading completed work. Use `--refresh` only when new provider content
must intentionally replace the versions selected by a new run.

## Atomic, content-addressed storage

The cache stores raw and normalized versions by content checksum:

```text
<cache>/
  manifest.json
  raw/<dataset>/<partition>/<sha256>.json
  normalized/<dataset>/<partition>/<sha256>.parquet
  runs/<run_id>.json
```

Raw JSON and normalized Parquet are written to temporary files and installed
atomically only after both writes succeed. `manifest.json` records each
partition's status, row count, schema version, paths, and SHA-256 checksums.
Reads verify status, file presence, safe relative paths, and both checksums.
Content-addressed filenames preserve prior partition versions rather than
overwriting them in place.

Each backfill also writes an immutable completed, failed, or interrupted run
manifest under `runs/`. It pins the partition versions observed by that run and
records the requested and effective ranges, allowing later work to identify
the exact input set even after `--refresh` creates newer content versions.

## Normalized units and market-cap fields

Tushare reports `circ_mv` in CNY 10,000 units and share counts in 10,000-share
units. Phase 1 deliberately preserves two different concepts:

```text
circ_market_cap_cny = circ_mv * 10,000
free_float_market_cap_cny = free_share * close * 10,000
```

`circ_market_cap_cny` represents circulating market capitalization, while
`free_float_market_cap_cny` uses the provider's true freely tradable share
count. They are not interchangeable. Normalization also reconciles circulating
market cap against `float_share * close * 10,000` within tolerance.

## Phase 1 scope

The foundation ingests exactly six datasets:

- `trade_cal`: SSE trading calendar;
- `stock_basic`: listed, delisted, and pending instrument master rows;
- `daily`: daily OHLCV and traded amount;
- `daily_basic`: daily share counts and market-cap fundamentals;
- `namechange`: effective-dated names used to reconstruct ST status; and
- `suspend_d`: daily suspension state.

Phase 1 reconstructs listed, delisted, ST, and suspended cases as of the
requested date. It intentionally excludes ranking or scoring, portfolio and
execution behavior, rights-issue and dividend adjustment logic, and
walk-forward fold orchestration. Those belong to later phases.

## Validation and fail-closed behavior

Normalization validates dataframe schemas, required values, dates, units,
finite numbers, natural-key uniqueness, and share-count ordering before a
partition can become complete. Snapshot reads additionally require verified
checksums for every needed partition.

An active row missing critical daily, daily-basic, or trailing-liquidity data
is marked ineligible with `missing_critical_data`; it is never allowed through
on optimistic defaults. Suspended rows are explicitly ineligible but count as
known for coverage. Snapshot coverage must be at least 95%; otherwise snapshot
construction raises an error instead of returning a partial universe.

## Provider data notice

Cached Tushare responses remain subject to the provider's terms. Do not
redistribute raw or normalized cached provider data. Share code, manifests
without provider content, and reproduction instructions instead.

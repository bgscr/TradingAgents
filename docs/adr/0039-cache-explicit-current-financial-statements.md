---
status: accepted
---

# Cache explicitly selected CURRENT financial statements

CURRENT personal research may explicitly select Tushare for `get_balance_sheet`,
`get_income_statement`, and `get_cashflow` through `tool_vendors`. This is a narrow
ordinary-analysis extension under `mainland_capability_routing_mode=legacy`, not
activation or bypass of the existing qualified statement adapters. It applies
only to authoritative mainland equities in CNY. Default routing and AKShare's
fundamentals summary remain unchanged.

The new adapter reuses FinancialToolDispatcher, source artifact normalization,
period completeness checks, and the existing Tushare Provider Request Coordinator
capacity identity. Each physical operation has one attempt. The shared operator
ceiling and cooldown remain binding. No qualification profile or historical
evidence packet is manufactured or changed.

## Cache semantics

Store immutable raw response observations in a separate SQLite file under
`data_cache_dir`, with active announcement-window selections. Scope binds the
account label, authoritative instrument revision, table endpoint, explicit HTTPS
destination/protocol, and normalizer version. The cache stores no credentials.
Do not use its current observations as strict historical PIT evidence.

The same announcement response serves annual and quarterly displays. Only missing
or expired intervals are fetched, preserving already requested history. Refresh
replaces current interval selections atomically and keeps historical observations.
All intervals, including old reports and successful empty responses, expire.
The default interval is 24 hours and is configurable. Forced refresh is explicit.
Failures preserve successful chunks and original source timestamps, with missing,
stale, and persistence diagnostics; no missing financial value becomes zero.

## Consequences

An explicit HTTPS destination is required before credential lookup or I/O. Neither
the SDK's HTTP default nor an unconfirmed destination is selected automatically.
The HTTPS implementation never follows redirects or retries. Only a cache miss or
refresh creates a client and reads `TUSHARE_TOKEN`; fresh cache hits do not.
An operator must use a distinct non-secret account scope label when changing
accounts. Selecting a route does not establish API entitlement or authorize a
particular live experiment.

Queries cover announcements from January 1 six years before the analysis year
through the analysis date. Tushare `start_date`/`end_date` filter announcement dates,
not report periods. Display at most five annual or eight quarterly periods that
pass the existing completeness/identity/unit gates and disclosure cutoff. Income
and cash flow use latest consolidated cumulative `report_type=1`; quarterly views
are not calculated single-quarter deltas. Missing report periods remain missing.

The active response cache is separate from market-history storage and qualified
provider-subrequest v1 keys. It does not change strict replay, preserved BLOCKED
outcomes, or historical reports/checkpoints. See [configuration and costs](../current-financial-statements.md).

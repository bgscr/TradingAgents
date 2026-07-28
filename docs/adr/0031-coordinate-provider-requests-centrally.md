---
status: accepted
---

# Coordinate provider requests centrally

All market-history network work will pass through a cross-process Provider Request Coordinator keyed by Upstream Service Identity rather than by caller or adapter name. Identical requests are single-flighted before acquiring capacity. Interactive mainland analysis has highest priority, followed by incremental refresh, explicitly configured prewarming, and Full History Reconciliation. Background prewarming is disabled until an operator configures an Operator Safety Ceiling.

## Consequences

Each upstream begins with one in-flight operation at most. Configured pacing is labeled local operator policy because no usable provider-published numeric request quota was found for AKShare/Eastmoney, BaoStock, or Yahoo. Cooldowns, circuit state, physical attempt counts, and valid `Retry-After` values are shared across processes and persisted; retries consume the same budget as first attempts. Yahoo HTTP 429 or `Too Many Requests` and BaoStock login-cap code `10001005` become typed capacity outcomes, while generic disconnects, empty frames, authentication failures, and malformed responses retain their distinct meanings.

Fallback remains sequential. The coordinator never fans a request out to all providers, shifts a burst onto the next provider, rotates identities or network origins, or probes until throttling to discover a limit. A provider already cooling down is skipped with a typed Source Acquisition Outcome; throttling never becomes a Source Fact.

## Decision basis

Primary-source findings and the distinction between published guidance and local safety policy are recorded in `docs/research/market-data-provider-rate-limits.md`.

---
status: accepted
---

# Derive mainland qfq history from raw prices and dated factors

The durable mainland source of truth will be unadjusted Raw Market Observations plus provider-specific Adjustment Factor Revisions. TradingAgents will derive each forward-adjusted (`qfq`) Authoritative Market Snapshot using only factors effective for its requested as-of date, with the raw observations, factor revisions, derivation version, and result digest recorded in Calculation Lineage.

## Consequences

A short refresh window over materialized `qfq` rows is insufficient because later dividends and splits can revise the entire earlier adjusted series. Provider-supplied adjusted frames remain useful for current fallback and equivalence verification, but they cannot establish historical point-in-time accuracy without dated factor lineage. Providers that cannot expose sufficient raw or factor history may still participate in the existing current-snapshot fallback during the mainland shadow rollout; their retrieved adjusted frames are stored as provider revisions and are not silently promoted to reconstructed point-in-time history.

## Validation evidence

Live BaoStock queries on 2026-07-25 returned identical 2023 `qfq` values whether the request ended in 2024 or 2026, while the same dates' unadjusted values differed. BaoStock separately exposed effective-dated adjustment factors through 2026. This demonstrates that its adjusted endpoint reflects current adjustment state rather than the nominal request cutoff and supports storing raw observations and factor events separately.

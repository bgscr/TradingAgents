---
status: accepted
---

# Use one provider frame for exact market claims

Each analysis will select one fully validated provider frame, using the configured market-specific fallback order, and derive all exact OHLCV and indicator claims from that frame under one recorded Adjustment Basis. Invalid rows are quarantined and cause fallback; data from separate providers is not blended because effective-date and adjustment differences can turn apparent reconciliation into a false trading signal.

## Consequences

For the default mainland configuration, the order remains AKShare, BaoStock, then Yahoo. Another exact market fact that contradicts the Authoritative Market Snapshot is excluded or blocks the decision when material.

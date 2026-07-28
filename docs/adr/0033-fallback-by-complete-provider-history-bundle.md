---
status: accepted
---

# Fall back by complete provider-history bundle

A strict point-in-time Authoritative Market Snapshot will be derived from one complete Provider History Bundle: Raw Market Observations, trading status, and Adjustment Factor Revisions bound to the same provider-specific upstream dataset and adjustment methodology. Provider selection and fallback occur at the bundle boundary. TradingAgents will not combine AKShare prices with BaoStock factors, Yahoo status with another provider's rows, or components merely because they share a Python adapter label.

## Consequences

A candidate missing any material bundle component is unavailable for strict historical reconstruction and acquisition proceeds sequentially to the next capable provider. A provider-adjusted frame that lacks dated factor lineage may remain an independently validated current-analysis fallback under existing semantics, labeled current-only, but it is not promoted to Observed Point-in-Time History. This strengthens rather than relaxes the single-provider rule in ADR-0002.

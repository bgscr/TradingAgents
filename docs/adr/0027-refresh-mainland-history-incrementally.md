---
status: accepted
---

# Refresh mainland market history incrementally

Ordinary mainland acquisition will fetch missing dates plus a Revision Refresh Window covering the latest 21 market sessions, rather than downloading the retained five-year range again. Adjustment-factor records are refreshed separately; when a factor changes, TradingAgents rebuilds the affected `qfq` history locally from stored Raw Market Observations and retains a new derived revision.

## Consequences

The 21-session overlap covers every close used by the current 20-trading-session return rule without changing that rule's horizon. A non-blocking monthly Full History Reconciliation checks the complete retained raw range for active mainland Instruments so older provider corrections are eventually detected. Reconciliation publishes corrections as new revisions and never overwrites prior observations. Maintenance failure is reported and retried but does not replace a still-valid pinned snapshot with partial data.

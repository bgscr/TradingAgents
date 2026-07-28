---
status: accepted
---

# Distinguish retrospective backfill from observed point-in-time history

The initial five-year seed and any later retrieval of previously unseen old data will be labeled Retrospective Backfill. Its observation date, provider-reported effective date, retrieval time, first-observed time, and revision identity remain distinct. A historical date alone does not permit TradingAgents to claim that the system or market participant possessed that exact revision at the time.

## Consequences

Retrospective Backfill may support current Trading Decisions and descriptive or current-as-of calculations over longer history. Strict historical decision replay and backtesting may use only Observed Point-in-Time History or a revision with authoritative availability evidence no later than the replay as-of time. Newly accumulated daily revisions become observed history from store activation onward. Snapshot and Calculation Lineage must expose the provenance class so downstream code cannot silently cross this trust boundary.

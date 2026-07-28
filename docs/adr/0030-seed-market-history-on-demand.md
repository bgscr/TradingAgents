---
status: accepted
---

# Seed market history on demand

TradingAgents will perform the five-year On-Demand History Seed when an Instrument is first analyzed. Background prewarming is limited to an explicitly configured portfolio or watchlist. The analysis pipeline will not automatically backfill every mainland-listed Instrument or infer a prewarming universe from registry membership or picker coverage.

## Consequences

Provider requests, storage growth, and maintenance work remain proportional to actual analysis demand. Monthly reconciliation and incremental refresh operate only on retained active or explicitly tracked Instruments. Prewarming is lower priority than interactive mainland analysis and must obey the same centralized provider request budgets, deduplication, cooldowns, and typed rate-limit outcomes.

---
status: accepted
---

# Drive mainland freshness from a persisted session calendar

TradingAgents will persist a versioned mainland Market Session Calendar, initially sourced through BaoStock and evaluated in `Asia/Shanghai`. A current analysis makes no price-provider request when retained history already contains the latest completed expected session. Before the current session is final, the preceding open session remains the expected effective date rather than treating an intraday absence as stale daily data.

## Consequences

The calendar belongs to the Market History Database and does not depend on the picker database. Calendar acquisition and refresh use the Provider Request Coordinator and are shared across Instruments. If an expected completed session's bar is not yet available, TradingAgents records a temporary Publication Watermark with a coordinated cooldown instead of repeatedly polling or manufacturing a missing-data fact. Weekends and known holidays cause no instrument-history request. Calendar revisions are retained and pinned like other operational history inputs.

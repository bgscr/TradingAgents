---
status: accepted
---

# Do not expand foreground acquisition for future replay

An interactive current analysis will use only its Foreground Acquisition Budget. Once a provider in the Current Analysis Provider Chain yields an accepted frame, TradingAgents stores that provider revision and proceeds; it will not call an additional provider solely to prepare strict historical replay, prewarming, or maintenance data.

## Consequences

If BaoStock is already selected and a complete bundle is available within the active coordinated request budget, the bundle may be retained without starting a second provider workflow. Otherwise a BaoStock strict-history seed runs only for an explicit historical-replay request or as configured lower-priority work. Background work cannot delay, duplicate, or jeopardize the current mainland analysis and pauses whenever foreground demand or an upstream cooldown exists.

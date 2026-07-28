---
status: accepted
---

# Use personal-research data mode for the local deployment

This deployment is personal, local to the operator's own machine, and does not redistribute market data or expose it as a service. TradingAgents will therefore use the `personal_research` Data Usage Mode, preserving the configured mainland provider chain and Yahoo `CCC` Crypto Reference Market under their visible source-policy notices and the centralized request controls in ADR-0031.

## Consequences

All cached history and provider artifacts remain local and are not treated as licensed redistribution assets. This engineering classification is not a grant of rights beyond provider terms. If the project is later operated commercially, shared as a hosted service, or used to redistribute data, it must switch to a distinct `production` mode in which only explicitly entitled providers participate; research-only providers then produce a typed unavailable outcome instead of being used silently. The open-source adapters remain available without claiming production entitlement.

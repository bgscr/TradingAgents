---
status: accepted
---

# Use instrument-specific observation calendars for return horizons

TradingAgents will keep the existing equity momentum horizon at 20 trading sessions, calculated from 21 market-session close observations. An explicitly confirmed suspension session counts in that sequence using its official carried close and zero volume, while remaining marked as suspended rather than implying that the Instrument traded. The initial crypto momentum horizon will be 20 calendar days, calculated from 21 consecutive daily closes including weekends. Calculation Definitions and Strategy Rules must identify the applicable instrument kind and observation calendar rather than interpreting a generic `20 days` globally.

## Consequences

Adding crypto does not change existing equity decision semantics. Acquisition may retain substantially more history than either rule requires, but the presence of additional observations does not silently widen a registered horizon. Missing required observations yield insufficient history under the applicable Calculation Definition rather than substituting another calendar.

---
status: accepted
---

# Block Trading Decisions for current mainland suspensions

When the latest applicable mainland market session authoritatively confirms that an Instrument remains suspended, TradingAgents will not issue a Buy, Hold, or Sell Trading Decision. The deterministic terminal result is a non-directional Analysis Outcome with reason `instrument_currently_suspended`, reporting the suspension and the latest genuinely traded close.

## Consequences

Current Tradeability is independent of historical evidence integrity: confirmed Suspension Observations remain valid inputs to registered calculations, but inability to trade cannot become a Hold premise or directional signal. A suspended outcome does not update directional trading memory. The status must come from authoritative provider or exchange evidence rather than inference from a blank or zero-volume row alone.

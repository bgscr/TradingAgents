---
status: accepted
---

# Fail calculations on in-range history gaps

An expected mainland session within an Instrument's listed lifecycle that has neither a validated observation nor an authoritative Suspension Observation is a History Gap. TradingAgents will not forward-fill, interpolate, or patch the missing row from another provider. A provider candidate with a History Gap inside a Calculation Definition's exact required input range falls back as a complete candidate or yields insufficient history.

## Consequences

Known market closures, dates before listing or after delisting, and confirmed suspensions are not History Gaps. A gap outside the exact input range of the current registered rule remains visible in coverage and lineage but does not block that otherwise valid calculation; a future longer-horizon rule crossing the same gap must fail independently. This preserves the single-provider boundary and prevents unrelated old incompleteness from silently disabling the current 20-session decision.

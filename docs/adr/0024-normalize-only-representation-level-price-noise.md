---
status: accepted
---

# Normalize only representation-level price noise

TradingAgents will apply a deterministic, versioned, provider-specific Representation Normalization before enforcing OHLC ordering. A discrepancy may be canonicalized only when it is attributable to serialization or binary floating-point representation and lies below a tightly bounded tolerance smaller than any meaningful market-price unit. The original values, normalized values, rule version, and reason are retained in Calculation Lineage and the normalized frame digest.

## Consequences

An effectively equal Yahoo `Low` and `Close` separated by one binary unit in the last place will no longer force rejection of an otherwise valid five-year mainland frame. Material OHLC violations, unexplained rounding, and missing values remain malformed and quarantine the provider candidate. Implementations must prefer declared source precision when available and cannot use broad rounding or tolerance to reconcile providers or conceal bad bars.

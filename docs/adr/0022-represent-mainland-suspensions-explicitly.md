---
status: accepted
---

# Represent mainland suspension sessions explicitly

An explicitly provider- or exchange-confirmed mainland suspension session will be retained as a Suspension Observation with suspended trading status, zero volume, and the official carried close. It counts as a market session in the equity 20-trading-session Observation Horizon but does not represent an executable trade. Provider blanks may be normalized this way only when authoritative status confirms the suspension; otherwise the row remains malformed and is quarantined.

## Consequences

The Point-in-Time Market History Store must preserve trading status and the provenance of any normalization in addition to OHLCV. This aligns equivalent BaoStock blank-volume suspension rows and Yahoo zero-volume suspension rows without treating arbitrary missing volume as zero. Current tradeability is a separate Decision Gate concern: this decision defines historical observation semantics only.

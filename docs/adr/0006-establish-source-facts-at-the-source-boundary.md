---
status: accepted
---

# Establish Source Facts at the source boundary

TradingAgents will have deterministic source adapters establish each Source Fact's canonical field identity, normalized value, unit or currency, instrument, effective date, adjustment basis, provenance, and calculation lineage. Analyst models may select and interpret Source Fact identifiers, but cannot establish a fact's meaning from a quote or matching number; this prevents semantically incorrect relabeling from passing a provenance-only check.

## Consequences

Raw provider payloads remain preserved for audit, while provider-specific adapters own normalization into a versioned fact vocabulary. Unsupported or ambiguous values remain diagnostics rather than becoming model-authored Source Facts.

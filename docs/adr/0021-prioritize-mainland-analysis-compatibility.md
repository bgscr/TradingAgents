---
status: accepted
---

# Prioritize mainland analysis compatibility

TradingAgents will treat mainland China analysis as the Primary Analysis Market and compatibility baseline for shared identity, acquisition, history, evidence, and decision infrastructure. US equities, Crypto Instruments, and other markets are secondary priorities. Work for a secondary market must not weaken or silently change mainland provider routing, forward-adjustment semantics, instrument capabilities, Calculation Definitions, Strategy Rules, or fail-closed decision behavior.

## Consequences

Market-history work must be designed and verified against Shanghai and Shenzhen equities before it becomes authoritative. Provider fallback and typed quarantine outcomes remain part of the mainland contract because live five-year validation demonstrated that no single configured provider covered every tested case cleanly. Any rollout must have an explicit compatibility gate and rollback path; adding another market is not sufficient justification for accepting a mainland regression.

The Point-in-Time Market History Store will therefore begin in shadow mode. The existing acquisition path remains authoritative and decision-producing while each accepted provider frame is written to the store and reconstructed from a pinned revision without making a second provider request. The Mainland Compatibility Gate compares Instrument Identity, provider, Adjustment Basis, effective range, eligible observations, normalized frame digest, derived Source Facts, and Calculation Lineage. It also exercises forced provider failures and the configured fallback order. Stored-history reads cannot become authoritative until these checks pass across the mainland test matrix, and cutover retains an immediate configuration rollback to the existing path. Secondary-market activation does not waive or precede this gate.

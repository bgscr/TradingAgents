# Trading Evidence and Decisions

This context defines the language used to describe market evidence and the point at which TradingAgents may issue a trading decision.

## Instruments

**Mainland Instrument**:
A security listed on a mainland Chinese exchange, identified by its exchange and instrument kind.
_Avoid_: A-share when referring to funds or indices

**Equity**:
A Mainland Instrument representing ownership in a company.
_Avoid_: Stock when the distinction from a fund matters

**Fund**:
A Mainland Instrument representing a pooled investment vehicle, including an exchange-traded fund.
_Avoid_: Company, A-share

## Evidence

**Source Fact**:
A fact preserved with its original source expression, normalized value, provenance, and effective date where applicable.
_Avoid_: Parsed value, LLM fact

**Material Evidence**:
Evidence whose absence, invalidity, or contradiction could change a trading decision or invalidate a calculation supporting it.
_Avoid_: Important data, critical feed

**Authoritative Market Snapshot**:
The single validated, consistently adjusted market history selected for all exact prices and derived indicators in one analysis.
_Avoid_: Verified row, blended snapshot

**Effective Trading Date**:
The latest trading date represented by an Authoritative Market Snapshot, which may precede the requested analysis date.
_Avoid_: Current date, report date

**Adjustment Basis**:
The corporate-action treatment applied consistently to a market-price history.
_Avoid_: Price mode

**Decision-Ready Evidence**:
Evidence that contains the minimum required market facts and validates every Material Evidence premise used by a proposed decision.
_Avoid_: Complete data, perfect data

**Degraded Evidence**:
Evidence that lacks optional sources or breadth but remains Decision-Ready for the proposed thesis.
_Avoid_: Partial failure

**Insufficient Evidence**:
Evidence that lacks a required fact or cannot support a material premise of the proposed thesis.
_Avoid_: Hold, failed analysis

**Conflicted Evidence**:
Evidence containing unresolved contradictory Source Facts that are material to the proposed thesis.
_Avoid_: Mixed sentiment, provider noise

## Outcomes

**Analysis Outcome**:
The completion state of an analysis, distinct from any directional trading recommendation.
_Avoid_: Trading decision, result code

**Trading Decision**:
A directional portfolio recommendation issued only from Decision-Ready Evidence.
_Avoid_: Analysis Outcome, Hold due to missing data

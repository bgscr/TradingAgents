# Trading Evidence and Decisions

This context defines the language used to describe market evidence and the point at which TradingAgents may issue a trading decision.

## Instruments

**Mainland Instrument**:
A security listed on a mainland Chinese exchange, identified by its exchange and instrument kind.
_Avoid_: A-share when referring to funds or indices

**Instrument Identity**:
An authoritative binding of a canonical symbol, trading venue, instrument kind, currency, and provenance; a display name is descriptive rather than identity-defining.
_Avoid_: Ticker string, company name

**Capability Profile**:
The evidence sources, analyses, and Strategy Rules applicable to an Instrument according to its instrument kind.
_Avoid_: Asset-type label, universal stock workflow

**Equity**:
A Mainland Instrument representing ownership in a company.
_Avoid_: Stock when the distinction from a fund matters

**Fund**:
A Mainland Instrument representing a pooled investment vehicle, including an exchange-traded fund.
_Avoid_: Company, A-share

## Evidence

**Canonical Evidence Contract**:
A versioned, closed representation of evidence accepted at a trusted boundary; preserved provider payloads and narrative prose outside the contract are not Decision-Ready Evidence.
_Avoid_: Raw payload schema, prompt format

**Evidence Preflight**:
A deterministic check that required baseline evidence can support model-mediated analysis before it begins; failure produces an Analysis Outcome without invoking analysts.
_Avoid_: Preliminary recommendation, optional-source completeness check

**Source Fact**:
A source-bound observation with canonical field identity, normalized value and unit, instrument and effective-date context, provenance, and calculation lineage. Its meaning is established at the source boundary rather than inferred from narrative prose.
_Avoid_: Parsed value, LLM fact

**Source Acquisition Outcome**:
A typed result of attempting to acquire source data that distinguishes an available artifact from an unavailable reason and its retryability; an unavailable outcome is diagnostic rather than evidence.
_Avoid_: Error-text fact, empty source

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

**Calculation Definition**:
A versioned deterministic specification of a derived fact's canonical inputs, formula, adjustment basis, history and warmup requirements, and output semantics.
_Avoid_: Indicator name, formula label

**Calculation Lineage**:
The binding of a derived Source Fact to its Calculation Definition and exact authoritative input artifact, effective range, observations, and implementation version.
_Avoid_: Data source, indicator label

**Decision-Ready Evidence**:
Evidence containing the minimum required market facts and supporting every Decision Assertion used to justify a proposed Trading Decision.
_Avoid_: Complete data, perfect data

**Strategy Rule**:
A versioned deterministic policy that relates canonical Source Facts to a directional premise for specified instrument kinds, horizons, comparators, and evidence requirements.
_Avoid_: Prompt heuristic, model opinion, trading rationale

**Decision Assertion**:
An auditable application of a registered Strategy Rule to validated Material Evidence that supports or opposes a proposed Trading Decision for an explicit horizon and comparator.
_Avoid_: Citation, fact binding, rationale prose

**Evidence Admission**:
The deterministic boundary after evidence collection that permits thesis synthesis only when canonical facts and artifacts are valid and at least one Decision Assertion candidate is Decision-Ready.
_Avoid_: Model confidence, report approval

**Validated Decision Context**:
The closed set of Source Facts, Decision Assertions, and fact-bound constraints permitted to influence the direction of a Trading Decision.
_Avoid_: Full conversation, debate transcript, memory context

**Degraded Evidence**:
Evidence that lacks optional sources or breadth but remains Decision-Ready for the proposed thesis.
_Avoid_: Partial failure

**Insufficient Evidence**:
Evidence that lacks a required fact or cannot support a material premise of the proposed thesis.
_Avoid_: Hold, failed analysis

**Conflicted Evidence**:
Evidence containing unresolved contradictory Source Facts that are material to the proposed thesis.
_Avoid_: Mixed sentiment, provider noise

**Source Availability Coverage**:
The share of required and optional acquisition capabilities that produced usable source artifacts, without implying decision sufficiency.
_Avoid_: Evidence confidence, decision confidence

**Validated Fact Coverage**:
The share of required canonical facts that satisfy integrity, freshness, conflict, and calculation requirements.
_Avoid_: Source count, claim count

**Decision Assertion Coverage**:
The completeness of valid rule-backed Decision Assertions for every premise permitted to influence direction; it must be complete for a Trading Decision.
_Avoid_: Citation count, rationale length

**Evidence Integrity Status**:
The categorical assurance state of evidence as Decision-Ready, Degraded, Insufficient, or Conflicted, distinct from predictive confidence.
_Avoid_: Decision Confidence, probability of success

## Outcomes

**Run Lifecycle Status**:
The execution state of an analysis run, independent of whether the run produces a directional decision.
_Avoid_: Decision status, evidence status

**Terminal Outcome Kind**:
The explicit classification of a completed run as producing either a Trading Decision or a non-directional Analysis Outcome.
_Avoid_: Completed flag, report type

**Analysis Outcome**:
The completion state of an analysis, distinct from any directional trading recommendation.
_Avoid_: Trading decision, result code

**Trading Decision**:
A directional portfolio recommendation issued only from Decision-Ready Evidence.
_Avoid_: Analysis Outcome, Hold due to missing data

**Decision Report**:
A deterministic human-readable rendering of a Trading Decision and its rule-backed Decision Assertions, including their semantic labels, evidence lineage, and integrity state.
_Avoid_: Model summary, thesis prose

**Advisory Commentary**:
Model-authored interpretation kept outside the Validated Decision Context that cannot determine a rating, signal, or memory update.
_Avoid_: Decision rationale, validated evidence

**Decision Gate**:
The final deterministic boundary that either permits a proposed Trading Decision from a Validated Decision Context or yields a non-directional Analysis Outcome.
_Avoid_: Portfolio manager opinion, output-format check

## Assurance

**Decision Metamorphic Test**:
An assurance scenario that transforms evidence or non-evidence context and checks the required relationship between the resulting decision contracts.
_Avoid_: Golden-output test, repeatability test

**Decision Invariance**:
The requirement that changes irrelevant to validated decision semantics leave the Validated Decision Context and normalized Trading Decision unchanged.
_Avoid_: Identical prose, deterministic model output

**Decision Sensitivity**:
The requirement that a material semantic change invalidates affected Decision Assertions and forces re-evaluation, a changed Trading Decision, or a non-directional Analysis Outcome.
_Avoid_: Any-output-difference test

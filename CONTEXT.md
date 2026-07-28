# Trading Evidence and Decisions

This context defines the language used to describe market evidence and the point at which TradingAgents may issue a trading decision.

## Instruments

**Primary Analysis Market**:
The mainland China market, whose existing Instrument Identity, provider routing, Adjustment Basis, evidence, and decision behavior form the compatibility baseline for shared-market changes. US equities, Crypto Instruments, and other markets are secondary priorities.
_Avoid_: Market-neutral rollout, feature parity at mainland compatibility's expense

**Mainland Instrument**:
A security listed on a mainland Chinese exchange, identified by its exchange and instrument kind.
_Avoid_: A-share when referring to funds or indices

**Instrument Identity**:
An authoritative binding of a canonical symbol, trading venue or Reference Market, instrument kind, currency, and provenance; a display name is descriptive rather than identity-defining.
_Avoid_: Ticker string, company name

**Reference Market**:
The authoritative market context that defines an Instrument's price semantics without necessarily being an execution venue. For initial Crypto Instruments, `CCC` is the aggregated Reference Market.
_Avoid_: Data provider, broker, executable exchange

**Instrument Registry**:
A versioned authority for Instrument Identities within one instrument family; Mainland Instruments and Crypto Instruments use independently governed registries. Analysis reads a pinned revision, while maintenance publishes a new revision only after candidate validation.
_Avoid_: Universal ticker map, provider metadata cache

**Capability Profile**:
The evidence sources, analyses, and Strategy Rules applicable to an Instrument according to its instrument kind.
_Avoid_: Asset-type label, universal stock workflow

**Equity**:
A Mainland Instrument representing ownership in a company.
_Avoid_: Stock when the distinction from a fund matters

**Fund**:
A Mainland Instrument representing a pooled investment vehicle, including an exchange-traded fund.
_Avoid_: Company, A-share

**Crypto Instrument**:
An Instrument representing a supported digital-asset and quote-currency pair. Both the base asset and quote currency are identity-defining, and identity is established authoritatively rather than inferred from its symbol.
_Avoid_: Crypto ticker, company, interchangeable USD/stablecoin pair

**Crypto Symbol Alias**:
An alternate spelling that preserves the same base asset and quote currency as a Crypto Instrument.
_Avoid_: Quote-currency substitution

**Crypto Trading Support**:
Full participation of a Crypto Instrument in the Canonical Evidence Contract, including eligibility for a Trading Decision when its evidence is Decision-Ready and its directional assertions are backed by applicable Strategy Rules.
_Avoid_: Ticker parsing, analysis-only crypto mode, legacy model signal

**Supported Crypto Universe**:
The Crypto Instruments admitted by the Crypto Instrument Registry. The initial universe is `BTC-USD`, `ETH-USD`, `SOL-USD`, `XRP-USD`, `ADA-USD`, `DOGE-USD`, `LTC-USD`, `BCH-USD`, `DOT-USD`, `AVAX-USD`, and `LINK-USD`.
_Avoid_: Any Yahoo-compatible pair, syntactically valid crypto symbol

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
The single validated, consistently adjusted market history selected for all exact prices and derived indicators in one analysis, constructed from a pinned revision of the Point-in-Time Market History Store without blending providers.
_Avoid_: Verified row, blended snapshot

**Point-in-Time Market History Store**:
A durable, provider-neutral repository of validated Raw Market Observations, trading status, Adjustment Factor Revisions, and their revisions. Acquisition fills missing ranges and refreshes an explicit revision window, while each Authoritative Market Snapshot pins the exact provider-specific inputs and Adjustment Basis it uses.
_Avoid_: Response cache, latest-values table, audit-artifact directory

**Market History Database**:
The dedicated SQLite operational index for the Point-in-Time Market History Store, containing observations, revisions, factors, status, provenance, ingestion state, and snapshot pins while referencing immutable provider payloads by content digest. It is separate from the picker database and audit-artifact storage.
_Avoid_: Picker cache, per-symbol CSV directory, payload blob dump

**History Store Degradation**:
The explicit current-analysis mode used when the Market History Database cannot safely serve or commit data. Mainland acquisition reverts to the existing live provider chain, constructs a complete single-provider snapshot, and reports the degradation; it never blends partial stored and live histories and is not permitted for strict historical replay.
_Avoid_: Cache miss, silent fallback, partial snapshot repair

**Retrospective Backfill**:
Historical market data first retrieved after its effective date. It may support current decisions and descriptive historical calculations, but it does not prove what data or revision was available for a decision at that earlier date.
_Avoid_: Observed point-in-time history, historical replay evidence

**Observed Point-in-Time History**:
A market-data revision whose authoritative availability or actual first-observed timestamp is no later than the analysis as-of time, making it eligible for strict historical decision replay subject to the applicable evidence contract.
_Avoid_: Any row with an old effective date, latest corrected history

**Revision Refresh Window**:
The bounded overlap re-acquired during ordinary incremental ingestion to detect recent provider corrections. For mainland daily history it contains the latest 21 market sessions in addition to any missing dates.
_Avoid_: Full-history download, strategy horizon expansion

**Market Session Calendar**:
A versioned, locally persisted authority for expected open and closed sessions in a Reference Market. The initial mainland calendar is sourced through BaoStock, interpreted in `Asia/Shanghai`, and remains separate from picker state.
_Avoid_: Weekday test, instrument price rows, picker-cache dependency

**Publication Watermark**:
The persisted result of determining whether an expected completed session's provider bar is available. A temporary not-yet-published watermark carries a cooldown and prevents repeated polling without being treated as market evidence.
_Avoid_: Missing-price fact, permanent gap, retry loop

**Full History Reconciliation**:
A non-blocking maintenance comparison of retained raw history against its provider, run monthly for active mainland Instruments to detect older corrections and publish them as new revisions.
_Avoid_: Daily analysis fetch, silent overwrite

**On-Demand History Seed**:
The first-use acquisition of up to five calendar years for one requested Instrument. Automatic seeding is limited to analyzed Instruments and explicitly configured portfolios or watchlists rather than crawling an entire market universe.
_Avoid_: Universe backfill, implicit watchlist, eager market crawl

**Foreground Acquisition Budget**:
The provider work required to satisfy the current interactive analysis only. It cannot be expanded to call another provider solely to prepare optional future replay or prewarming data.
_Avoid_: Opportunistic provider crawl, background work in request latency, unused fallback

**Upstream Service Identity**:
The actual remote capacity boundary shared by provider calls, such as Eastmoney `push2his`, the BaoStock TCP service and account, or Yahoo Finance. Multiple local adapters reaching the same service share one request budget and cooldown.
_Avoid_: Python function, analyst, provider display name

**Provider History Bundle**:
The provider- and upstream-specific set of Raw Market Observations, trading status, and Adjustment Factor Revisions required to derive one strict point-in-time market history. Bundle fallback is atomic; components from different providers or upstream adjustment methodologies are never combined.
_Avoid_: Best field from each provider, adapter-name grouping, blended adjustment inputs

**Current Analysis Provider Chain**:
The configured sequential provider order used to obtain a present-day Authoritative Market Snapshot. For mainland Instruments it remains AKShare, BaoStock, then Yahoo and is distinct from eligibility for strict historical reconstruction.
_Avoid_: Parallel fan-out, strict-replay provider list, fastest provider

**Strict History Provider**:
A provider whose complete Provider History Bundle is qualified for strict historical reconstruction. Initial mainland support uses BaoStock; qualification does not reorder the Current Analysis Provider Chain.
_Avoid_: Current fallback, adjusted-only provider, blended bundle

**Operator Safety Ceiling**:
A locally configured conservative request ceiling for an Upstream Service Identity. It limits TradingAgents behavior but is not represented as a provider-published quota, permission, or guaranteed safe rate.
_Avoid_: Provider allowance, discovered maximum, entitlement

**Provider Request Coordinator**:
The cross-process scheduler that deduplicates physical requests, enforces Operator Safety Ceilings and persistent cooldowns by Upstream Service Identity, and prioritizes interactive mainland analysis over refresh, prewarming, and reconciliation.
_Avoid_: Per-run retry loop, parallel fallback, rate-limit bypass

**Data Usage Mode**:
The operator-declared boundary governing which configured providers may be used. This local deployment uses `personal_research`; a future `production` deployment requires explicit provider entitlement and must not inherit research-only adapters silently.
_Avoid_: Rate-limit setting, inferred license, universal provider permission

**Suspension Observation**:
An explicitly provider- or exchange-confirmed mainland market session during which an Instrument did not trade, represented with suspended status, zero volume, and its official carried close. It advances a market-session Observation Horizon but does not assert that an executable trade occurred; an unconfirmed blank row is not a Suspension Observation.
_Avoid_: Missing data, stale quote, synthetic trade

**History Gap**:
An expected Instrument session within its listed lifecycle that has neither a validated market observation nor an authoritative Suspension Observation. Its materiality is determined by the exact input range of a Calculation Definition; it is never silently forward-filled, interpolated, or patched from another provider.
_Avoid_: Holiday, pre-listing date, zero-return session

**Effective Trading Date**:
The latest trading date represented by an Authoritative Market Snapshot, which may precede the requested analysis date.
_Avoid_: Current date, report date

**Current Tradeability**:
The authoritative status of whether an Instrument could trade in its latest applicable market session, distinct from evidence sufficiency and directional merit. A currently suspended Mainland Instrument is not tradeable even when its historical evidence is otherwise valid.
_Avoid_: Liquidity score, Hold premise, evidence availability

**Adjustment Basis**:
The corporate-action treatment applied consistently to a market-price history.
_Avoid_: Price mode

**Adjustment Factor Revision**:
A provider-specific, dated and versioned corporate-action factor used to derive an adjusted market history from Raw Market Observations. Its effective date and retrieval provenance determine which as-of snapshots may use it.
_Avoid_: Latest adjustment multiplier, overwritten factor

**Raw Market Observation**:
A validated provider-specific OHLCV observation before corporate-action adjustment, retained immutably with its effective date, retrieval provenance, and revision identity.
_Avoid_: Adjusted close, provider-independent price

**Representation Normalization**:
A versioned, provider-specific canonicalization of numeric values that differ only because of serialization or binary floating-point representation. It preserves original values and its rule in lineage and is bounded below any meaningful market-price unit; it is not permission to repair materially invalid market data.
_Avoid_: Data repair, blanket rounding, approximate validation

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

**Observation Horizon**:
The instrument-specific sequence of closes required by a Calculation Definition or Strategy Rule. The initial equity return rule uses 21 market-session close observations, including confirmed Suspension Observations, for a 20-trading-session return; the initial crypto return rule uses 21 consecutive daily closes, including weekends, for a 20-calendar-day return.
_Avoid_: Universal 20-day window, fetched-history range

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

**Mainland Compatibility Gate**:
The pre-cutover proof that a shadow Point-in-Time Market History Store reconstructs the current authoritative mainland snapshot and its derived facts without changing Instrument Identity, provider, Adjustment Basis, eligible observations, lineage, or decision semantics, including under forced provider fallback. The current acquisition path remains authoritative until the gate passes and retains an immediate rollback path after cutover.
_Avoid_: Successful write, generic integration test, crypto-first validation

**Decision Metamorphic Test**:
An assurance scenario that transforms evidence or non-evidence context and checks the required relationship between the resulting decision contracts.
_Avoid_: Golden-output test, repeatability test

**Decision Invariance**:
The requirement that changes irrelevant to validated decision semantics leave the Validated Decision Context and normalized Trading Decision unchanged.
_Avoid_: Identical prose, deterministic model output

**Decision Sensitivity**:
The requirement that a material semantic change invalidates affected Decision Assertions and forces re-evaluation, a changed Trading Decision, or a non-directional Analysis Outcome.
_Avoid_: Any-output-difference test

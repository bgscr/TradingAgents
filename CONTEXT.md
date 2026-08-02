# Trading Evidence and Decisions

This document establishes the canonical language, evidence constraints, and decision policies for TradingAgents.

---

## 1. Instruments

| Term | Definition & Rules | Disallowed Terms (_Avoid_) |
| :--- | :--- | :--- |
| **Primary Analysis Market** | Mainland China market. Baseline for shared-market logic. US equities and Crypto are secondary. | Market-neutral rollout, feature parity at mainland compatibility's expense |
| **Mainland Instrument** | Security listed on a mainland Chinese exchange (XSHG/XSHE), identified by exchange and kind. | A-share (when referring to funds or indices) |
| **Instrument Identity** | Authoritative binding of canonical symbol, venue/Reference Market, kind, currency, and provenance. Display name is descriptive only. | Ticker string, company name |
| **Reference Market** | Authoritative market context defining price semantics (e.g., `CCC` for aggregated Crypto). | Data provider, broker, executable exchange |
| **Instrument Registry** | Versioned authority for identities. Pinned revision read during analysis; updated only after validation. | Universal ticker map, provider metadata cache |
| **Capability Profile** | Applicable evidence sources, analyses, and Strategy Rules for a specific instrument kind. | Asset-type label, universal stock workflow |
| **Equity** | Mainland Instrument representing company ownership. | Stock (when distinction from fund matters) |
| **Fund** | Mainland Instrument representing pooled investment vehicle (including ETFs). | Company, A-share |
| **Crypto Instrument** | Digital-asset and quote-currency pair. Identity established authoritatively, not inferred from symbol. | Crypto ticker, company, interchangeable USD/stablecoin pair |
| **Crypto Symbol Alias** | Alternate spelling preserving the exact same base asset and quote currency. | Quote-currency substitution |
| **Crypto Trading Support** | Full participation in Canonical Evidence Contract (eligible for Trading Decision when Decision-Ready). | Ticker parsing, analysis-only crypto mode, legacy model signal |
| **Supported Crypto Universe**| Admitted universe: `BTC-USD`, `ETH-USD`, `SOL-USD`, `XRP-USD`, `ADA-USD`, `DOGE-USD`, `LTC-USD`, `BCH-USD`, `DOT-USD`, `AVAX-USD`, `LINK-USD`. | Any Yahoo-compatible pair, syntactically valid crypto symbol |

---

## 2. Evidence

| Term | Definition & Rules | Disallowed Terms (_Avoid_) |
| :--- | :--- | :--- |
| **Canonical Evidence Contract** | Versioned, closed evidence representation at a trusted boundary. Unpreserved payloads/narratives are not Decision-Ready. | Raw payload schema, prompt format |
| **Evidence Preflight** | Deterministic check supporting model analysis before execution. Failure stops run without calling analysts. | Preliminary recommendation, optional-source completeness check |
| **Source Fact** | Source-bound observation with canonical field, normalized value/unit, effective date, provenance, and lineage. | Parsed value, LLM fact |
| **Source Acquisition Outcome** | Typed acquisition result distinguishing available artifact from unavailable reason and retryability. | Error-text fact, empty source |
| **Material Evidence** | Evidence whose absence, invalidity, or contradiction could change a trading decision or supporting calculation. | Important data, critical feed |
| **Authoritative Market Snapshot** | Single validated, consistently adjusted history for exact prices/indicators from pinned PIT store revision without provider blending. | Verified row, blended snapshot |
| **Point-in-Time Market History Store** | Provider-neutral store of Raw Market Observations, trading status, and Adjustment Factor Revisions. | Response cache, latest-values table, audit-artifact directory |
| **Market History Database** | SQLite operational index for the PIT store (observations, revisions, factors, snapshot pins). Separate from picker DB. | Picker cache, per-symbol CSV directory, payload blob dump |
| **History Store Degradation** | Mode when Market History DB cannot safely serve/commit. Reverts to live chain, builds single-provider snapshot, reports degradation. | Cache miss, silent fallback, partial snapshot repair |
| **Retrospective Backfill** | Data retrieved after its effective date. Supports current decisions/descriptive calculations, but does not prove past availability. | Observed point-in-time history, historical replay evidence |
| **Observed Point-in-Time History** | Revision whose available/observed timestamp is $\le$ analysis as-of time. Eligible for strict replay. | Any row with an old effective date, latest corrected history |
| **Revision Refresh Window** | Bounded overlap re-acquired during ingestion (21 sessions for mainland daily) to detect provider corrections. | Full-history download, strategy horizon expansion |
| **Market Session Calendar** | Versioned authority for open/closed sessions (BaoStock / `Asia/Shanghai`). Separate from picker state. | Weekday test, instrument price rows, picker-cache dependency |
| **Publication Watermark** | Persisted check of completed session bar availability. Not-yet-published status carries cooldown; prevents polling loops. | Missing-price fact, permanent gap, retry loop |
| **Full History Reconciliation** | Non-blocking monthly comparison of raw history against provider for active mainland instruments to detect corrections. | Daily analysis fetch, silent overwrite |
| **On-Demand History Seed** | First-use acquisition up to 5 years for analyzed instruments/configured watchlists. No full-universe crawling. | Universe backfill, implicit watchlist, eager market crawl |
| **Foreground Acquisition Budget** | Provider work required for current interactive analysis only. Cannot call another provider solely for future prewarming. | Opportunistic provider crawl, background work in request latency |
| **Upstream Service Identity** | Actual remote capacity boundary (e.g., Eastmoney `push2his`, BaoStock TCP). Local adapters share budget/cooldown. | Python function, analyst, provider display name |
| **Provider History Bundle** | Set of Raw Observations, status, and Adjustment Factors from one provider. Fallback is atomic; never combine providers. | Best field from each provider, adapter-name grouping |
| **Current Analysis Provider Chain** | Sequential provider order for present-day snapshot (Mainland: AKShare $\rightarrow$ BaoStock $\rightarrow$ Yahoo). | Parallel fan-out, strict-replay provider list, fastest provider |
| **Strict History Provider** | Provider qualified for strict historical reconstruction (e.g., BaoStock). Does not reorder Current Chain. | Current fallback, adjusted-only provider, blended bundle |
| **Operator Safety Ceiling** | Configured conservative request ceiling for an Upstream Service Identity. Not a provider-published quota. | Provider allowance, discovered maximum, entitlement |
| **Provider Request Coordinator** | Cross-process scheduler enforcing Safety Ceilings, cooldowns, and deduplication. Prioritizes interactive runs. | Per-run retry loop, parallel fallback, rate-limit bypass |
| **Data Usage Mode** | Operator-declared license boundary (`personal_research` vs `production`). | Rate-limit setting, inferred license, universal provider permission |
| **Suspension Observation** | Session where instrument did not trade (suspended status, 0 volume, carried close). Advances Observation Horizon. | Missing data, stale quote, synthetic trade |
| **History Gap** | Session with neither validated observation nor Suspension Observation. Never silently forward-filled or patched across providers. | Holiday, pre-listing date, zero-return session |
| **Effective Trading Date** | Latest trading date in an Authoritative Snapshot (may precede requested analysis date). | Current date, report date |
| **Current Tradeability** | Authoritative status of whether instrument could trade in latest session. Suspended mainland instruments are not tradeable. | Liquidity score, Hold premise, evidence availability |
| **Adjustment Basis** | Corporate-action treatment applied consistently to price history (e.g., `qfq`). | Price mode |
| **Adjustment Factor Revision** | Provider-specific, dated corporate-action factor used to derive adjusted history. | Latest adjustment multiplier, overwritten factor |
| **Raw Market Observation** | Validated OHLCV observation before corporate-action adjustment, retained immutably. | Adjusted close, provider-independent price |
| **Representation Normalization** | Canonicalization of numeric serialization differences. Bounded below market-price units; not for data repair. | Data repair, blanket rounding, approximate validation |
| **Calculation Definition** | Versioned specification of derived fact formula, inputs, adjustment basis, and history/warmup needs. | Indicator name, formula label |
| **Calculation Lineage** | Binding of a derived Source Fact to its Definition, input artifact, effective range, and implementation version. | Data source, indicator label |
| **Decision-Ready Evidence** | Evidence containing required market facts and supporting every Decision Assertion for a proposed decision. | Complete data, perfect data |
| **Strategy Rule** | Versioned deterministic policy relating Source Facts to a directional premise for specified horizons/comparators. | Prompt heuristic, model opinion, trading rationale |
| **Observation Horizon** | Required sequence of closes (Mainland: 21 close observations for 20-session return; Crypto: 21 daily closes for 20-day return). | Universal 20-day window, fetched-history range |
| **Decision Assertion** | Auditable application of a Strategy Rule to Material Evidence supporting/opposing a decision. | Citation, fact binding, rationale prose |
| **Evidence Admission** | Boundary permitting thesis synthesis only when facts are valid and at least one Assertion candidate is Decision-Ready. | Model confidence, report approval |
| **Validated Decision Context** | Closed set of Source Facts, Assertions, and constraints allowed to influence Trading Decision direction. | Full conversation, debate transcript, memory context |
| **Degraded Evidence** | Evidence lacking optional sources but remaining Decision-Ready for the proposed thesis. | Partial failure |
| **Insufficient Evidence** | Evidence lacking a required fact or unable to support a material premise. Yields non-directional Outcome. | Hold, failed analysis |
| **Conflicted Evidence** | Evidence containing unresolved contradictory Material Facts. | Mixed sentiment, provider noise |
| **Source Availability Coverage**| Share of required/optional acquisition capabilities producing usable artifacts. | Evidence confidence, decision confidence |
| **Validated Fact Coverage** | Share of required canonical facts satisfying integrity, freshness, conflict, and calculation rules. | Source count, claim count |
| **Decision Assertion Coverage** | Completeness of valid rule-backed Assertions for every premise; must be 100% for a Trading Decision. | Citation count, rationale length |
| **Evidence Integrity Status** | Categorical assurance state: `Decision-Ready`, `Degraded`, `Insufficient`, or `Conflicted`. | Decision Confidence, probability of success |

---

## 3. Outcomes

| Term | Definition & Rules | Disallowed Terms (_Avoid_) |
| :--- | :--- | :--- |
| **Run Lifecycle Status** | Execution state of an analysis run, independent of directional decision generation. | Decision status, evidence status |
| **Terminal Outcome Kind** | Explicit classification of completed run: `Trading Decision` vs non-directional `Analysis Outcome`. | Completed flag, report type |
| **Analysis Outcome** | Completion state of an analysis run when no directional recommendation is issued. | Trading decision, result code |
| **Trading Decision** | Directional portfolio recommendation issued **only** from Decision-Ready Evidence. | Analysis Outcome, Hold due to missing data |
| **Decision Report** | Deterministic rendering of a Trading Decision and rule-backed Assertions with evidence lineage. | Model summary, thesis prose |
| **Advisory Commentary** | Model-authored commentary outside Validated Context; cannot determine rating, signal, or memory log. | Decision rationale, validated evidence |
| **Decision Gate** | Final deterministic boundary permitting a Trading Decision or yielding an Analysis Outcome. | Portfolio manager opinion, output-format check |

---

## 4. Assurance

| Term | Definition & Rules | Disallowed Terms (_Avoid_) |
| :--- | :--- | :--- |
| **Mainland Compatibility Gate** | Proof that shadow PIT store reconstructs mainland snapshot without changing Identity, provider, basis, or lineage. | Successful write, generic integration test, crypto-first validation |
| **Decision Metamorphic Test** | Assurance test transforming context to verify required contract relationships across runs. | Golden-output test, repeatability test |
| **Decision Invariance** | Requirement that non-semantic context changes leave Validated Context and Decision unchanged. | Identical prose, deterministic model output |
| **Decision Sensitivity** | Requirement that material semantic changes invalidate Assertions and force re-evaluation. | Any-output-difference test |
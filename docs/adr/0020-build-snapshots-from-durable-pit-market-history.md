---
status: accepted
---

# Build authoritative snapshots from durable point-in-time market history

TradingAgents will persist validated daily Raw Market Observations, explicit trading status, and applicable Adjustment Factor Revisions for supported instruments in a provider-neutral Point-in-Time Market History Store. The first acquisition for an instrument seeds up to five calendar years of available daily history, and validated observations are retained indefinitely. Subsequent acquisition fills missing ranges and refreshes an explicit recent-revision window instead of re-fetching the full overlapping range. Provider corrections are retained as revisions rather than silently overwriting prior observations, and each Authoritative Market Snapshot is constructed from a pinned, provider-specific revision set under one Adjustment Basis.

## Consequences

The store becomes the operational source for equity and crypto market snapshots while preserving the single-provider rule; provider-neutral storage does not permit cross-provider row blending. A newly listed instrument seeds only the history that exists and does not manufacture a five-year record. Snapshot lineage records the exact observations and revisions used so current analysis can consume corrections and historical analysis can avoid lookahead. Content-addressed evidence artifacts remain immutable audit records, and the picker ingestion database remains a separate subsystem. Accumulated history becomes available to future Calculation Definitions and Strategy Rules, but existing registered horizons do not expand automatically.

Operational indexing and immutable payload retention follow ADR-0028; neither the picker database nor per-symbol response-cache files become the market-history authority.

## Validation evidence

On 2026-07-25, the current authoritative provider chain was exercised over 2021-07-25 through 2026-07-25 for seven liquid mainland equities spanning the Shanghai main board, STAR Market, Shenzhen main board, SME, and ChiNext. Every symbol returned 1,211 validated observations from 2021-07-26 through 2026-07-24 with no duplicate dates or null OHLCV cells in the accepted frame. Six snapshots selected BaoStock and one selected Yahoo, demonstrating that the range is retrievable but that fallback remains material.

The same run exposed three compatibility cases the store must not obscure: AKShare's remote endpoint disconnected; BaoStock represented one STAR Market suspension with blank volume and was quarantined by the current validator; and one Yahoo frame contained a one-unit-in-the-last-place OHLC ordering difference and was quarantined. These are provider-specific acquisition or validation outcomes, not permission to blend rows across providers.

# 10 — Implement capability-specific routing and deterministic per-period selection

**What to build:** Make qualified_v1 choose the best compatible mainland dataset at the correct capability boundary: financial statements per statement/period, indicators per field/family/period, factors as sequential datasets, BaoStock-authoritative status, and split name versus ST/lifecycle history. A provider or period failure must never erase already accepted independent periods, and fallback must stop once target coverage is complete.

**Blocked by:** 01 — Add the qualified_v1 Capability Routing Plan and configuration preflight; 02 — Add provider-neutral financial period and completeness contracts; 03 — Add coordinator endpoint scope and checkpoint-safe provider subrequest caching; 04 — Implement Tushare balance, income, and cash-flow adapters; 05 — Implement the Tushare financial-indicator adapter; 06 — Implement Tushare adjustment-factor and name-event adapters; 07 — Implement AKShare-Sina statement adapters with partial-result salvage; 08 — Expose BaoStock raw, factor, status, and lifecycle capabilities; 09 — Implement BaoStock qualified ratio-family adapters.

**Status:** ready-for-agent

## Observable behavior

- Daily snapshot remains AKShare → BaoStock → Yahoo. Statements route Tushare → AKShare-Sina → Yahoo per normalized statement/period. Indicators route Tushare → AKShare → only missing BaoStock families → Yahoo degraded current snapshot.
- Factors route BaoStock → Tushare → AKShare → Yahoo-derived degraded; strict replay remains bundle-atomic. BaoStock alone is authoritative for suspension/trading status. Name events route Tushare → AKShare, while dated ST/listing/delisting metadata is BaoStock-first.
- The existing financial dispatcher/compiled tool node owns one immutable sequential candidate/selection plan. Each artifact is persisted before evaluation; accepted periods survive sibling failure; fallback continues only for missing/rejected/conflicted targets; overlapping later periods remain retained but unselected unless needed.
- Capability, company type, unit, currency, consolidation scope, period identity, filing metadata, and completeness must be compatible. No silent conversion, coercion, market-row blending, strict-bundle blending, or ratio-as-statement substitution.

## Acceptance criteria

- [ ] **CR4–CR7:** Exact route order, independent statement acquisition, partial-provider salvage, and per-period fallback are deterministic with exact physical attempts.
- [ ] **CR8–CR15:** Company-type, five/eight-period, since-listing, metadata, compatibility, conflict, and AKShare partial-salvage rules determine selection per statement/period.
- [ ] **CR16–CR18:** Tushare indicators remain current-analysis bounded, only missing BaoStock families are requested, Yahoo is final degraded snapshot, and ratio artifacts cannot satisfy statements.
- [ ] **CR19–CR23:** Factor/status/name/lifecycle routes and authority/degradation boundaries match the production matrix exactly; ADR-0023 suspension behavior and strict Provider History Bundle rules remain unchanged.
- [ ] **CR24–CR27:** Fallback is sequential, cache-aware, single-flighted, and exact-attempt counted with no hidden adapter retries or fan-out.
- [ ] **CR30–CR31:** Exhausted required statement coverage returns typed unavailable/Insufficient Evidence; optional indicator gaps may be Degraded only when remaining evidence is Decision-Ready.
- [ ] Public financial tools, non-mainland behavior, legacy mode, Current Analysis Provider Chain, Evidence Admission, Decision Gate, Strategy Rules, and parent **AC1–AC5 and AC9–AC13** remain compatible.

## Verification commands

```powershell
pytest -q tests/test_financial_tool_dispatcher.py tests/test_financial_graph_dispatch.py tests/test_akshare_data.py tests/test_baostock_data.py tests/test_tushare_pit_provider.py
pytest -q tests/test_market_snapshot.py tests/test_market_history_reconstruction.py tests/test_current_tradeability.py tests/test_provider_request_coordinator.py tests/test_evidence_gates.py
pytest -q -m "not integration"
ruff check .
git diff --check
```

## Definition of done

- [ ] Deterministic bank, non-bank, Shanghai, Shenzhen, recent-listing, overlap, conflict, and complete/partial/failure fixtures prove the full routing matrix and selection algorithm.
- [ ] Model-facing rendering derives only from final selected periods and preserves existing tool-call correlation/public contracts.
- [ ] Legacy remains default; qualified_v1 is opt-in; no global Tushare-first route, authoritative Tushare suspension, cross-provider strict bundle, live call, payload/secret, or AKShare upgrade appears.
- [ ] Focused and compatibility commands pass with exact route order and physical-attempt cardinality assertions.


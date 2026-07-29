# 08 — Expose BaoStock raw, factor, status, and lifecycle capabilities

**What to build:** Make BaoStock’s qualified raw prices, factors, per-session trading status, dated ST status, and listing lifecycle metadata explicitly available to capability routing while preserving the existing complete BaoStock Provider History Bundle as the authoritative strict-history and Current Tradeability boundary.

**Blocked by:** 01 — Add the qualified_v1 Capability Routing Plan and configuration preflight; 02 — Add provider-neutral financial period and completeness contracts; 03 — Add coordinator endpoint scope and checkpoint-safe provider subrequest caching.

**Status:** ready-for-agent

## Observable behavior

- Explicit capabilities cover raw daily observations, forward factors, `tradestatus`, dated `isST`, IPO/listing status, and out/delisting metadata using coordinated BaoStock requests and immutable artifacts.
- Strict reconstruction still selects one complete BaoStock Provider History Bundle atomically; factor/status components from other providers can never be blended into it.
- BaoStock dated status alone establishes Current Tradeability and ST facts. Lifecycle metadata is authoritative for since-listing eligibility and excludes pre-listing periods.
- Existing market-snapshot, history-store, pin, Adjustment Basis, and suspension terminal semantics are unchanged.

## Acceptance criteria

- [ ] **CR19–CR20:** A complete BaoStock factor capability remains first/strict-bundle compatible; a later provider factor cannot replace missing BaoStock bundle components for strict replay.
- [ ] **CR22:** Deterministic traded and suspended fixtures prove BaoStock status controls Current Tradeability; supplemental empty Tushare events create no traded fact.
- [ ] **CR23:** Dated BaoStock `isST` may establish status, while names alone cannot.
- [ ] Authoritative listing metadata drives the **CR11** since-listing contract and records provenance; IPO/out/delisting observations remain distinct.
- [ ] Parent **AC1–AC5** and **ADR-0023** pass unchanged, including exact `instrument_currently_suspended`, latest genuinely traded close, v2 pin membership, OPIT/backfill, and strict replay behavior.
- [ ] Parent **AC9–AC13** prove coordinated calls, legacy persistence, clean runtime state, and no provider reorder/fan-out.

## Verification commands

```powershell
pytest -q tests/test_baostock_data.py tests/test_market_snapshot.py tests/test_market_history_store.py tests/test_market_history_reconstruction.py tests/test_current_tradeability.py
pytest -q tests/test_provider_request_coordinator.py tests/test_checkpoint_resume.py tests/test_decision_audit.py
pytest -q -m "not integration"
ruff check .
git diff --check
```

## Definition of done

- [ ] Deterministic Shanghai, Shenzhen, traded, suspended, ST, listing, and delisting fixtures prove each exposed capability and its authority boundary.
- [ ] Complete BaoStock strict-history bundle semantics and ADR-0023 are byte/behavior compatible at public seams.
- [ ] No Tushare authority, cross-provider bundle blending, provider fan-out, live call, raw payload/error, credential, or unrelated history-store refactor is introduced.
- [ ] Focused and compatibility commands pass, including exact physical-attempt assertions.


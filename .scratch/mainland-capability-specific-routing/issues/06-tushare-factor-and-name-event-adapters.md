# 06 — Implement Tushare adjustment-factor and name-event adapters

**What to build:** Expose qualified Tushare native adjustment factors and dated name events as two bounded capabilities that can participate in later fallback, while making it impossible for either output—or an empty suspension event response—to establish authoritative trading status, ST state, or a strict Provider History Bundle.

**Blocked by:** 01 — Add the qualified_v1 Capability Routing Plan and configuration preflight; 02 — Add provider-neutral financial period and completeness contracts; 03 — Add coordinator endpoint scope and checkpoint-safe provider subrequest caching.

**Status:** ready-for-agent

## Observable behavior

- Real single-stock `adj_factor` and `namechange` adapters retain immutable artifacts and emit only native dated Adjustment Factor Revisions or dated name events, respectively.
- A Tushare factor never claims raw-observation/status bundle completeness or Strict History Provider authority.
- Name events preserve dated old/new names and provider lineage but cannot create `isST`, Current Tradeability, suspension, listing, or delisting facts—even when text contains `ST` or `*ST`.
- No authoritative `suspend_d` route is added. Any existing event-only ingestion remains supplemental; an empty event set creates no traded fact.

## Acceptance criteria

- [ ] **CR20:** A valid Tushare factor is usable only by qualified factor/current analysis and cannot construct or repair a strict Provider History Bundle.
- [ ] **CR23:** Ordinary, G-prefix, ST-looking, and `*ST` name fixtures retain events but create no ST/tradeability Source Fact without authoritative dated status.
- [ ] **CR24–CR25:** Both endpoints use exact typed outcomes, endpoint-aware cooldowns, and duplicate single-flight identities with no adapter retry.
- [ ] Empty `suspend_d` fixture is supplemental/non-authoritative and produces no status assertion, preserving **CR22**, **ADR-0023**, and parent **AC1–AC2** behavior.
- [ ] Legacy mode and daily market routing remain unchanged; parent **AC3–AC5, AC9–AC11, and AC13** strict-history/coordination/compatibility rules still pass.

## Verification commands

```powershell
pytest -q tests/test_tushare_pit_provider.py tests/test_market_history_reconstruction.py tests/test_current_tradeability.py tests/test_evidence_gates.py tests/test_provider_request_coordinator.py
pytest -q -m "not integration"
ruff check .
git diff --check
```

## Definition of done

- [ ] Deterministic factor, name, ST-looking-name, empty-event, and typed failure fixtures prove the capability boundaries.
- [ ] Adapter artifacts are immutable and manifest-ready, with no strict-bundle, status-authority, or Source Fact overclaim.
- [ ] No live provider/model call, token, raw payload/error, VIP interface, AKShare upgrade, or global Tushare-first route appears.
- [ ] Focused and compatibility commands pass; review explicitly checks ADR-0023 and strict-history atomicity.


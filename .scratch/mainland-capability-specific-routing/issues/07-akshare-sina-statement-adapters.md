# 07 — Implement AKShare-Sina statement adapters with partial-result salvage

**What to build:** Replace mainland statement placeholders with real AKShare-Sina balance, income, and cash-flow acquisition so each actual subrequest produces its own artifact and usable periods survive sibling failures. Yahoo must remain an observable dispatcher-level fallback, never a hidden adapter call.

**Blocked by:** 01 — Add the qualified_v1 Capability Routing Plan and configuration preflight; 02 — Add provider-neutral financial period and completeness contracts; 03 — Add coordinator endpoint scope and checkpoint-safe provider subrequest caching.

**Status:** ready-for-agent

## Observable behavior

- Each requested statement uses the current qualified AKShare package and real Sina financial-report endpoint through the Provider Request Coordinator.
- Each actual remote subrequest has one immutable artifact/outcome. Report/announcement dates, currency, scope/type, and supplied update fields are retained; missing `f_ann_date` or stable revision identity is explicit rather than inferred.
- Bank/non-bank normalization and completeness use ticket 02 contracts. A failed statement, frequency projection, or supplemental subrequest does not erase usable sibling artifacts or periods.
- The adapter never calls Yahoo or substitutes AKShare indicators for statement fields; fallback remains dispatcher-owned and sequential.

## Acceptance criteria

- [ ] **CR6–CR7:** Independent usable statements/periods survive endpoint or period failure and retain exact artifact/outcome lineage.
- [ ] **CR8–CR14:** Company type, completeness, since-listing, identity compatibility, overlap/conflict, and filing metadata rules apply identically to AKShare-Sina candidates.
- [ ] **CR15:** A usable AKShare-Sina period survives another AKShare subrequest failure; no internal Yahoo call occurs and dispatcher-level fallback remains visible.
- [ ] Every physical subrequest is coordinated, single-flighted where identical, and counted exactly; no SDK retry or unrecorded secondary call occurs.
- [ ] Existing AKShare version, daily market role, legacy routes, public tools, and parent **AC9–AC13** behavior remain unchanged.

## Verification commands

```powershell
pytest -q tests/test_akshare_data.py tests/test_financial_tool_dispatcher.py tests/test_financial_graph_dispatch.py tests/test_provider_request_coordinator.py tests/test_checkpoint_resume.py
pytest -q -m "not integration"
ruff check .
git diff --check
```

## Definition of done

- [ ] Deterministic bank/non-bank, three-statement, partial failure, missing metadata, and malformed response fixtures pass.
- [ ] Placeholders are removed only for the three qualified statement capabilities; returned artifacts/candidates obey shared contracts.
- [ ] No AKShare/dependency upgrade, internal Yahoo fallback, provider fan-out, live call, raw payload/error, credential, or unrelated refactor is present.
- [ ] Focused and compatibility commands pass with exact physical-attempt assertions.


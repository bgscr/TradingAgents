# 09 — Implement BaoStock qualified ratio-family adapters

**What to build:** Fill only missing financial-indicator families and periods through BaoStock profit, operation, growth, balance, cash-flow, and DuPont adapters, preserving usable families when another is sparse or fails and categorically preventing ratio data from satisfying a full-statement request.

**Blocked by:** 01 — Add the qualified_v1 Capability Routing Plan and configuration preflight; 02 — Add provider-neutral financial period and completeness contracts; 03 — Add coordinator endpoint scope and checkpoint-safe provider subrequest caching.

**Status:** ready-for-agent

## Observable behavior

- Each family emits capability `financial_ratio_family` plus its exact family and normalized company-type field set.
- The dispatcher can request explicit missing family/year/quarter keys only. Each distinct key is one coordinated physical attempt/cache entry; a satisfied family/period is never re-requested.
- Usable growth or DuPont periods survive sparse/failed operation or cash-flow families. Every family—including bank operation/cash-flow—must pass eight periods and no more than 10% declared-set missingness; there is no bank waiver.
- Passing any ratio artifact to balance, income, or cash-flow selection yields typed `capability_mismatch` and continues statement fallback.

## Acceptance criteria

- [ ] **CR17:** Only missing qualified BaoStock families/periods are requested after higher-priority indicator gaps; usable families survive sibling sparsity/failure.
- [ ] **CR18:** Every ratio-to-statement attempt is rejected as `capability_mismatch`, retained operationally, and cannot satisfy statement completeness.
- [ ] **CR27:** Physical attempts equal exactly the distinct missing family/year/quarter keys across duplicate logical requests and checkpoint resume.
- [ ] Bank/non-bank deterministic fixtures enforce declared family fields, eight reporting periods, and ≤10% missingness without response-derived schemas.
- [ ] Existing BaoStock strict-history bundle, status authority, legacy financial behavior, Evidence Admission, Decision Gate, and parent **AC9–AC13** remain unchanged.

## Verification commands

```powershell
pytest -q tests/test_baostock_data.py tests/test_financial_tool_dispatcher.py tests/test_provider_request_coordinator.py tests/test_checkpoint_resume.py tests/test_evidence_gates.py
pytest -q -m "not integration"
ruff check .
git diff --check
```

## Definition of done

- [ ] Deterministic fixtures cover all six families, mature bank/non-bank coverage, missing-only acquisition, sparse sibling salvage, duplicate requests, and capability mismatch.
- [ ] Every physical request is coordinator-owned with exact cache/attempt identity and no hidden retry or fan-out.
- [ ] No statement adapter substitution, live provider/model call, credential, raw payload/error, ratio-field overclaim, or unrelated BaoStock refactor is present.
- [ ] Focused and compatibility commands pass.


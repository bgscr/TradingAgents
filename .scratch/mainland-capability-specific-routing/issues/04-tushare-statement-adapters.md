# 04 — Implement Tushare balance, income, and cash-flow adapters

**What to build:** Allow an enabled qualified_v1 run to acquire each full financial statement from Tushare as one coordinated single-stock history request, retain the immutable artifact, and expose independently usable annual/reporting period candidates with complete filing and company-type lineage.

**Blocked by:** 01 — Add the qualified_v1 Capability Routing Plan and configuration preflight; 02 — Add provider-neutral financial period and completeness contracts; 03 — Add coordinator endpoint scope and checkpoint-safe provider subrequest caching.

**Status:** ready-for-agent

## Observable behavior

- Real `balancesheet`, `income`, and `cashflow` adapters use only qualified single-stock interfaces and one bulk endpoint/symbol/as-of request per statement; annual and reporting projections reuse that artifact.
- Every decoded response is stored immutably before normalization. Original fields/values/units and `ann_date`, `f_ann_date`, `report_type`, `comp_type`, `update_flag`, retrieval time, dataset identity, and local revision identity remain available.
- Company type and consolidation scope are resolved through ticket 02 contracts. Monetary normalization occurs only for an explicit endpoint scale; ambiguous units, scope, metadata, or duplicate revisions remain typed candidates/rejections rather than silently merged values.
- SDK failures are sanitized into exact typed outcomes. No token, raw exception, raw payload, VIP interface, internal retry, or uncoordinated request is allowed.

## Acceptance criteria

- [ ] **CR5:** Complete Tushare histories yield independent balance, income, and cash-flow target candidates/artifacts with filing metadata; one complete statement does not cause another endpoint call.
- [ ] **CR6–CR7:** Failure of one endpoint or rejection of one period preserves every usable independent statement/period and records exact attempts/outcomes.
- [ ] **CR8–CR12:** Bank/non-bank cores, target periods, since-listing exception inputs, and unit/currency/scope/period rejection behave through the shared contracts.
- [ ] **CR14:** Filing metadata survives adapter serialization and checkpoint round-trip; duplicate/update candidates retain distinct local revision identities without overstating restatement lineage.
- [ ] **CR24 and CR26:** Typed provider outcomes are exact, and annual/reporting views of one response produce one physical attempt.
- [ ] Legacy mode and configurations without Tushare financial enablement never instantiate or call these adapters; parent **AC9–AC11 and AC13** remain unchanged.

## Verification commands

```powershell
pytest -q tests/test_tushare_pit_provider.py tests/test_financial_tool_dispatcher.py tests/test_checkpoint_resume.py tests/test_provider_request_coordinator.py
pytest -q -m "not integration"
ruff check .
git diff --check
```

## Definition of done

- [ ] Deterministic Shanghai non-bank, Shenzhen non-bank, bank, and recent-listing fixtures cover success, partial rejection, metadata, duplicate revision, and typed failure paths.
- [ ] All three adapters return contract-valid artifacts/candidates without implementing cross-provider selection or model-facing routing.
- [ ] No live qualification, token, raw payload, cache, AKShare upgrade, VIP call, or production suspension/status route is introduced.
- [ ] Focused and compatibility commands pass, and review confirms exact coordinated physical-call cardinality.


# 13 — Run joint deterministic acceptance and parent regression validation

**What to build:** Prove the complete qualified_v1 tracer bullet through the real deterministic CLI/compiled-graph seams, then prove the unchanged parent market-data safety contract twice. This is the rollout gate: any CR1–CR38 or parent AC1–AC13 failure blocks completion even if individual adapter tests pass.

**Blocked by:** 01 — Add the qualified_v1 Capability Routing Plan and configuration preflight; 02 — Add provider-neutral financial period and completeness contracts; 03 — Add coordinator endpoint scope and checkpoint-safe provider subrequest caching; 04 — Implement Tushare balance, income, and cash-flow adapters; 05 — Implement the Tushare financial-indicator adapter; 06 — Implement Tushare adjustment-factor and name-event adapters; 07 — Implement AKShare-Sina statement adapters with partial-result salvage; 08 — Expose BaoStock raw, factor, status, and lifecycle capabilities; 09 — Implement BaoStock qualified ratio-family adapters; 10 — Implement capability-specific routing and deterministic per-period selection; 11 — Add financial manifests, evidence disposition, reporting, and artifact retention; 12 — Add qualified_v1 shadow rollout, activation, rollback, and legacy compatibility.

**Status:** ready-for-agent

## Observable behavior

- A deterministic end-to-end matrix covers bank, mature Shanghai/Shenzhen non-bank, and recent-listing Instruments through authoritative identity, fake coordinated transports, routing/selection, immutable artifacts, checkpoint round-trip, reports/audits, Evidence Admission, Decision Gate, strict replay, CLI terminal output, and rollback.
- Fixtures cover complete and sparse histories; partial salvage; permission/auth/rate-limit/empty/malformed/timeout outcomes; duplicate updates; missing first-publication; conflicts; incompatible units/currency/scope/company type; factor/status/name boundaries; and exact provider-attempt cardinality.
- No scenario calls a live Tushare or other provider, uses a paid model, reads qualification caches/raw payloads, or requires a credential.

## Acceptance criteria

- [ ] **CR1–CR38:** Every capability-routing criterion in the authoritative spec has a named deterministic test at the highest relevant public seam and passes without waiver.
- [ ] Parent **AC1–AC13** pass unchanged, including ADR-0023 suspension terminal behavior, OPIT/retrospective strict history, atomic artifact publication/GC, crypto registry behavior, ADR-0031 exact attempts/cooldowns, legacy migration/readability, clean-tree behavior, and full regression scope.
- [ ] Full suite passes twice from the same clean code state to demonstrate deterministic reproducibility; Ruff and `git diff --check` pass.
- [ ] Compatibility review confirms legacy default/output/checkpoints, daily AKShare → BaoStock → Yahoo order, BaoStock strict/status authority, Evidence Admission, Decision Gate, Strategy Rules, reports, snapshots, pins, artifacts, and provider records are unchanged.
- [ ] Scope review confirms no live qualification, token/credential/payload/cache, AKShare/dependency upgrade, global Tushare-first route, authoritative Tushare suspension, cross-provider strict bundle, provider fan-out, hidden retry, or unrelated refactor.

## Verification commands

```powershell
pytest -q tests/test_financial_tool_dispatcher.py tests/test_financial_graph_dispatch.py tests/test_akshare_data.py tests/test_baostock_data.py tests/test_reporting.py tests/test_checkpoint_resume.py
pytest -q tests/test_provider_request_coordinator.py tests/test_mainland_physical_attempt_coordination.py tests/test_market_snapshot.py tests/test_market_history_store.py tests/test_market_history_reconstruction.py tests/test_market_history_shadow.py tests/test_current_tradeability.py tests/test_evidence_artifacts.py tests/test_evidence_gates.py tests/test_admitted_evidence_binding.py tests/test_decision_audit.py tests/test_recorded_evidence_replay.py tests/test_tushare_pit_provider.py
pytest -q -m "not integration"
1..2 | ForEach-Object { pytest -q; if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE } }
ruff check .
git diff --check
git status --short
```

## Definition of done

- [ ] A CR1–CR38 traceability record and parent AC1–AC13 result record identify the deterministic test(s) and outcome for every criterion.
- [ ] The complete fixture matrix, focused suites, non-integration suite, full suite twice, Ruff, and whitespace validation pass.
- [ ] **Stage 1 — Spec Compliance Review:** confirms routes, authority boundaries, contracts, migrations, rollout, compatibility, acceptance IDs, and all out-of-scope constraints; any functional gap is fixed before Stage 2.
- [ ] **Stage 2 — Code Quality Review:** confirms boundary separation, typed/sanitized errors, atomic persistence, exact attempts, test quality, simplicity, security/evidence safety, and no unrelated changes; all Critical/Important findings are resolved.
- [ ] The final diff contains no credentials, tokens, token hashes, raw provider payloads/errors, local caches, qualification reruns, or production-state rewrites, and qualified_v1 remains ready for opt-in rollout with immediate legacy rollback.


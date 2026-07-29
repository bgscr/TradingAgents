# 01 — Add the qualified_v1 Capability Routing Plan and configuration preflight

**What to build:** Make capability-specific mainland routing an explicit, secret-free run contract that is resolved after authoritative Instrument Identity and before graph or model work. Existing operators continue to receive legacy behavior by default; an operator may opt into a closed `qualified_v1` plan only when its Tushare capability, profile, token boundary, Data Usage Mode, dependency, pacing, and request-budget configuration is coherent.

**Blocked by:** None — can start immediately.

**Status:** ready-for-agent

## Observable behavior

- `legacy` remains the default and preserves every registered provider chain and public tool/output contract.
- A valid `qualified_v1` plan has deterministic, versioned capability routes, qualification profile `cn-a-2000-20260729-v1`, policy identities, budgets, and a stable signature. Its safe projection contains no token, token digest, exception text, raw payload, process identity, or call-order dependency.
- Tushare enablement is closed to statements, financial indicators, adjustment factors, and name events. It cannot enable suspension/status, cannot become globally first, and never enters the daily market-snapshot chain.
- Invalid enabled/token/profile/mode/dependency/pacing combinations return a typed Analysis Outcome before graph construction, provider requests, or model calls.

## Acceptance criteria

- [ ] **CR1:** Default and explicit legacy configurations preserve existing financial behavior, AKShare → BaoStock → Yahoo daily snapshots, checkpoints, and output contracts.
- [ ] **CR2:** Every invalid `qualified_v1` configuration case fails typed before graph/model work with zero provider/model calls and secret-safe rendering.
- [ ] **CR3:** Repeated construction from semantically identical inputs produces the same closed plan and signature regardless of mapping order, process, or token value; material route/policy changes change the signature.
- [ ] **CR4:** A qualified mainland daily snapshot still calls AKShare → BaoStock → Yahoo sequentially and never calls Tushare.
- [ ] **CR35:** A clean-tree default run activates no new Tushare route and preserves existing generated-artifact behavior.
- [ ] Compatibility proof covers existing configurations without Tushare financial enablement and leaves parent **AC2, AC9, AC10, AC11, AC12, and AC13** behavior unchanged.
- [ ] Deterministic tests use fake providers/models and environment-bound dummy secrets only; no live provider, paid model, credential, raw payload, or local cache is required.

## Verification commands

```powershell
pytest -q tests/test_dataflows_config.py tests/test_financial_graph_dispatch.py tests/test_market_snapshot.py tests/test_checkpoint_resume.py tests/test_cli_config_precedence.py tests/test_cli_run_status.py
pytest -q -m "not integration"
ruff check .
git diff --check
```

## Definition of done

- [ ] The plan/preflight behavior and deterministic tests satisfy CR1–CR4 and CR35 at the highest existing seams.
- [ ] The plan is additive, checkpoint/audit-safe, secret-free, and `legacy` remains the default.
- [ ] No production route is globally Tushare-first; no Strategy Rule, Decision Gate, Evidence Admission rule, provider retry rule, or AKShare dependency changes.
- [ ] Focused and compatibility commands pass, and a scoped self-review finds no credential exposure or unrelated refactor.


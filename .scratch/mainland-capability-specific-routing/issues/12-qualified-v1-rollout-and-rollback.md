# 12 — Add qualified_v1 shadow rollout, activation, rollback, and legacy compatibility

**What to build:** Give operators a reversible path from legacy routing to explicit qualified_v1 shadow observation and then selection, without migrating or deleting historical state. Configuration rollback must affect new runs immediately, while each existing run/checkpoint remains bound to the immutable plan with which it started.

**Blocked by:** 01 — Add the qualified_v1 Capability Routing Plan and configuration preflight; 10 — Implement capability-specific routing and deterministic per-period selection; 11 — Add financial manifests, evidence disposition, reporting, and artifact retention.

**Status:** ready-for-agent

## Observable behavior

- Shipping/default mode is `legacy`. Explicit shadow mode records qualified candidate manifests/completeness for designated validation workloads while legacy selected financial output stays authoritative; ordinary analysis does not perform shadow prewarming or extra foreground calls.
- Qualified selection activates only under the coherent opt-in plan from ticket 01 and only for enabled capabilities. Disabling it makes the next run use legacy immediately.
- Rollback performs no schema downgrade, artifact deletion, report/checkpoint rewrite, provider-record rewrite, or route migration; prior qualified artifacts remain readable.
- Legacy checkpoints resume only under compatible legacy identities. A qualified_v1 mismatch fails typed before model work and starts a new run rather than upgrading the checkpoint in place.

## Acceptance criteria

- [ ] **CR1–CR3:** Default legacy, invalid preflight, and immutable safe-plan behavior hold through CLI/run startup and resume paths.
- [ ] **CR33:** Legacy checkpoint resume remains readable; qualified mismatch fails typed and starts a new run with zero reuse/rewrite of incompatible state.
- [ ] **CR35:** A clean default run activates no Tushare financial route and leaves existing snapshots/reports/provider records and ignore behavior unchanged.
- [ ] **CR36:** After qualified artifacts exist, rollback changes new-run selection to legacy immediately while retaining all prior data byte-for-byte.
- [ ] Shadow tests prove legacy remains authoritative, requests occur only in explicit validation workloads, and no hidden provider/model calls occur in ordinary analysis.
- [ ] Parent **AC6, AC9–AC13** migration, coordination, checkpoint, clean-tree, and full-regression behavior remains unchanged.

## Verification commands

```powershell
pytest -q tests/test_cli_config_precedence.py tests/test_cli_run_status.py tests/test_financial_graph_dispatch.py tests/test_checkpoint_resume.py tests/test_reporting.py
pytest -q tests/test_market_history_shadow.py tests/test_market_history_gitignore.py tests/test_provider_request_coordinator.py tests/test_recorded_evidence_replay.py
pytest -q -m "not integration"
ruff check .
git diff --check
git status --short
```

## Definition of done

- [ ] Deterministic default, shadow, activate, invalid-resume, and rollback scenarios pass without live providers or paid models.
- [ ] Rollout is opt-in and immediately reversible; every run is plan-bound and legacy artifacts/checkpoints remain immutable/readable.
- [ ] No global Tushare-first state, Tushare suspension authority, AKShare upgrade, deletion/migration rollback, payload/secret exposure, or speculative prewarming exists.
- [ ] Focused and compatibility commands pass, and generated runtime state follows existing repository rules.


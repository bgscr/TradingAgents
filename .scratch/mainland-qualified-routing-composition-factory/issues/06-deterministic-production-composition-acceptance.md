# 06 — Run deterministic production-composition acceptance

**Parent specification:** [Mainland qualified routing production composition factory](../spec.md)

**What to build:** Prove the complete shipped production-composition tracer
bullet through the real deterministic CLI and programmatic host builders, graph,
checkpoint-aware dispatcher, qualified router, production adapters, shared
Provider Subrequest Cache, Provider Request Coordinator, immutable artifacts,
checkpoint/report/audit publication, and managed cleanup. Produce a traceability
record for QCF1–QCF18, rerun PE1–PE15 and parent AC1–AC13 unchanged, run the full
suite twice, and complete Spec Compliance review before Code Quality review.
Any failure blocks Ticket 07.

**Blocked by:** 03 — Add static registration and caller precedence; 02D — Add
executable production-source wiring; 04A — Integrate managed eligibility-context
acquisition; 05A — Bind and restore eligibility context through checkpoints.

**Status:** blocked

**Acceptance state:** FAIL/BLOCKED and not complete. Ticket 06 cannot resume
until 02A, 02B, 02C, 02D, 04A, and 05A are complete.

## Observable behavior

- Fixed clocks, fake sleepers, fake SDKs/transports, disposable existing stores,
  retained immutable artifacts, and fake models exercise the shipped built-in
  selection and real compiled-graph/dispatcher seams without a test factory or
  dynamic-import fixture.
- QCF4–QCF12 each begin from a new built-in run with no manually seeded
  eligibility context, callable runtime, or checkpoint. The run must originate
  and bind its own immutable context before ordinary factory/dispatcher use.
- The matrix covers complete and partial Tushare, AKShare-Sina salvage, Yahoo
  degraded/current-only terminal fallback, BaoStock tradeable/suspended and
  strict-bundle authority, factor/name separation, ratio families, exact
  physical attempts, single-flight, bulk reuse, checkpoint restoration,
  security canaries, and every construction/execution cleanup boundary.
- Legacy-selected financial output remains authoritative, qualified output is
  always \`shadow_non_authoritative\`, daily mainland routing remains AKShare →
  BaoStock → Yahoo, and authoritative \`qualified_v1\` remains disabled.
- Representative historical reports, checkpoints, artifacts, attempts,
  manifests, audits, snapshots, pins, provider rows, and IDs remain
  byte-for-byte untouched and readable.

## Acceptance criteria

QCF1–QCF18 remain unchanged. QCF4–QCF12 must originate from a fresh built-in
run. A manually seeded Financial Eligibility Acquisition Context, callable
runtime, or checkpoint cannot satisfy fresh acceptance for any of QCF4–QCF12.

- [ ] **QCF1 — Built-in reachability:** CLI and programmatic explicit validation
  select the statically registered built-in without injection/factory path;
  graph receives the managed plan-bound composition and
  \`qualified_routing_composition_missing\` is absent.
- [ ] **QCF2 — Override precedence:** direct composition invokes neither
  factory; explicit operator factory wins over built-in; safe-config and
  plan-signature validation remain exact.
- [ ] **QCF3 — Zero-work/authority controls:** legacy, ordinary non-validation
  shadow, empty/non-applicable plan, and \`qualified_v1\` controls construct no
  qualified resource or I/O; authority-disabled preflight precedes built-in
  construction.
- [ ] **QCF4 — Complete Tushare:** complete statement/indicator fixtures retain
  all eligible targets/families, call no lower provider, and record exactly the
  Tushare endpoint calls.
- [ ] **QCF5 — Tushare gaps:** successful artifacts/periods survive unavailable
  endpoints and partial gaps; only unresolved work continues; no successful
  bulk acquisition repeats per target.
- [ ] **QCF6 — AKShare-Sina salvage:** AKShare-Sina is called sequentially only
  for unresolved statements, retains usable partial results, and prevents Yahoo
  calls for resolved targets.
- [ ] **QCF7 — Yahoo degraded fallback:** Yahoo remains last, coordinated, and
  selected only under existing current-only/degraded contracts; candidate-free
  profile and derived-factor behavior do not acquire strict/native authority.
- [ ] **QCF8 — BaoStock authority:** raw/factor/status/lifecycle/ratio fixtures
  cover tradeable and suspended sessions; only BaoStock establishes Current
  Tradeability, strict three-component bundle identity remains atomic, and
  ratios are requested narrowly.
- [ ] **QCF9 — Factor/name routes:** factor and issuer-name calls retain distinct
  plan routes, endpoint scopes, keys, artifacts, attempts, and authority while
  sharing one cache; names remain descriptive and bundles are never blended.
- [ ] **QCF10 — Exact attempts:** complete, retryable, terminal, cooldown-skip,
  partial-fallback, and restored-cache matrices for every assembled provider
  give one event per real transport call, exact configured retries/fallback,
  zero events for non-physical work, and no hidden retry/fan-out.
- [ ] **QCF11 — Single-flight/bulk reuse:** identical concurrent subrequests
  share one physical sequence and terminal result; one bulk artifact serves all
  covered targets/calls with deterministic reuse; materially different keys do
  not collide.
- [ ] **QCF12 — Exact restoration:** reconstruction through the built-in restores
  plan, eligibility, dispatcher/cache, artifacts, candidates, selections,
  attempts, and IDs exactly; repeated work performs zero provider/network/model
  I/O or coordinator permits; corrupt/mismatched controls fail typed.
- [ ] **QCF13 — Pre-open validation:** every missing credential/dependency and
  invalid mode/plan/asset/pacing/Safety Ceiling/account scope/Data Usage
  Mode/runtime root/source coverage/output case fails secret-free before any
  graph/model/provider/store/artifact/report/attempt work.
- [ ] **QCF14 — Lifecycle cleanup:** failure after every construction step and
  during graph initialization, dispatch, execution, checkpoint publication, and
  \`KeyboardInterrupt\` preserves the original error; interrupt/close is
  idempotent; all sessions/connections/stores/leases/workers are released or
  terminally abandoned.
- [ ] **QCF15 — Security canaries:** tokens, credential-like text, credentialed
  URLs, and raw provider exceptions appear nowhere in config, plan, repr, IDs,
  events, exceptions, logs, reports, checkpoints, artifacts, manifests, audits,
  or terminal output; only existing safe typed diagnostics cross boundaries.
- [ ] **QCF16 — Authority invariance:** legacy output, qualified shadow output,
  daily acquisition, Evidence Admission, Decision Gate, report, checkpoint, and
  decision audit comparisons show unchanged legacy/daily authority and
  \`shadow_non_authoritative\` qualified output.
- [ ] **QCF17 — Historical immutability:** representative historical material is
  byte-for-byte unchanged/readable; new runs use existing schemas/identities
  with no migration, rewrite, deletion, relabeling, or renumbering.
- [ ] **QCF18 — Unchanged parent assurance:** PE1–PE15 and parent AC1–AC13 pass
  with their existing text, fixtures, expected results, and authority, followed
  by the full supported suite twice, Ruff, whitespace, and diff/scope checks,
  all without waiver.

## Deterministic regressions

- Run every focused test added by Tickets 01–05, 02A, 02B, 02C, 02D, 04A, and
  05A plus existing adapter, coordinator/cache, rollout, checkpoint, graph-
  dispatch, report, audit, Evidence Admission, Decision Gate, Current
  Tradeability, strict-history, and daily-market tests.
- Cover exact attempt cardinality for success, retry, capacity cooldown, skip,
  terminal failure, sequential fallback, cache hit, restored hit, single-flight,
  and bulk reuse for every assembled provider.
- Exercise secret canaries and raw-provider-exception canaries across all public
  and persisted boundaries.
- Exercise cleanup at every construction and execution boundary under ordinary
  exceptions, \`KeyboardInterrupt\`, \`SystemExit\`, repeated close, and cleanup
  failure.
- Run PE1–PE15 and parent AC1–AC13 from their authoritative specifications
  without altering fixtures or expected results.
- Run the complete supported suite twice from the identical code tree and
  compare deterministic results.

## Compatibility and migration behavior

- Acceptance uses disposable new run/checkpoint namespaces and existing
  content-addressed artifact behavior. It does not clear, recycle, repair, or
  overwrite historical state.
- Legacy and pre-v2 checkpoints retain existing read/start-new/fail-typed
  behavior. No schema, ID, provider record, snapshot, pin, report, or artifact is
  upgraded in place.
- Existing Capability Routing Plan, completeness, financial eligibility,
  Evidence Admission, Decision Gate, Strategy Rules, BaoStock status/lifecycle/
  strict-history authority, and daily market route are treated as immutable
  baselines.
- Passing deterministic acceptance is evidence only. It neither enables
  \`qualified_v1\` nor authorizes live rollout by itself.

## Explicit non-goals

- Live Tushare, AKShare, Yahoo, or BaoStock calls; paid models; live
  qualification caches; real credentials; or the two-symbol rollout.
- Enabling/recommending \`qualified_v1\`, changing activation gates, provider
  routes/order, thresholds, eligibility, revision selection, evidence/decision
  authority, BaoStock authority, daily routing, or shadow disposition.
- New router, cache, persistence authority, checkpoint sidecar, report schema,
  provider registry, ID namespace, historical migration, provider fan-out,
  hidden retry, account rotation, or speculative prewarming.
- Waiving or editing QCF1–QCF18, PE1–PE15, or parent AC1–AC13 to obtain a pass.

## Verification commands

Run production-composition and provider focused suites:

\`\`\`powershell
pytest -q tests/test_qualified_routing_composition_contract.py tests/test_production_qualified_provider_composition.py tests/test_qualified_routing_static_registration.py tests/test_qualified_routing_managed_lifecycle.py tests/test_qualified_routing_factory_checkpoint_restore.py
pytest -q tests/test_financial_eligibility_acquisition_context.py tests/test_financial_eligibility_metadata_acquisition.py tests/test_financial_request_factory_context_binding.py
pytest -q tests/test_qualified_production_source_bindings.py tests/test_qualified_routing_eligibility_context_lifecycle.py tests/test_qualified_routing_checkpoint_context_restore.py
pytest -q tests/test_tushare_statement_adapters.py tests/test_tushare_indicator_adapter.py tests/test_tushare_factor_name_adapters.py tests/test_akshare_sina_statement_adapters.py tests/test_baostock_capability_adapters.py tests/test_baostock_ratio_adapters.py
pytest -q tests/test_provider_subrequest_cache.py tests/test_provider_request_coordinator.py tests/test_mainland_physical_attempt_coordination.py tests/test_yahoo_retry_coordination.py
\`\`\`

Run graph, checkpoint, shadow-authority, PE1–PE15, and parent AC1–AC13
regressions:

\`\`\`powershell
pytest -q tests/test_financial_tool_dispatcher.py tests/test_financial_graph_dispatch.py tests/test_financial_capability_routing_selection.py tests/test_qualified_routing_rollout.py tests/test_corrected_shadow_joint_acceptance.py
pytest -q tests/test_checkpoint_resume.py tests/test_financial_version_persistence.py tests/test_financial_manifest_evidence_reporting.py tests/test_reporting.py tests/test_decision_audit.py tests/test_admitted_evidence_binding.py tests/test_recorded_evidence_replay.py tests/test_evidence_gates.py
pytest -q tests/test_market_snapshot.py tests/test_market_history_store.py tests/test_market_history_reconstruction.py tests/test_market_history_shadow.py tests/test_current_tradeability.py
pytest -q -m "not integration"
\`\`\`

Run the complete suite twice and final checks:

\`\`\`powershell
1..2 | ForEach-Object { pytest -q; if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE } }
ruff check .
git diff --check
git status --short
\`\`\`

No command in this ticket may select integration tests or contact a live
provider/model. If a test would do so, stop and correct the deterministic seam
instead of running it.

## Definition of done

- [ ] A durable traceability record maps QCF1–QCF18, PE1–PE15, and parent
  AC1–AC13 to named deterministic tests, commands, evidence, and passing results.
- [ ] Focused suites, non-integration suite, full suite twice, Ruff, whitespace,
  secret/payload, historical-immutability, and scope checks all pass without
  waiver.
- [ ] **Stage 1 — Spec Compliance Review:** confirms the exact production
  contract, routes, eligibility, ownership, restoration, authority,
  compatibility, acceptance IDs, blocking edges, and every explicit non-goal.
  Any functional gap is resolved before Stage 2.
- [ ] **Stage 2 — Code Quality Review:** after Stage 1 passes, confirms error
  handling, lifecycle durability, concurrency, typed/sanitized boundaries,
  deterministic tests, reuse of existing seams, maintainability, and absence of
  unrelated changes. All Critical and Important findings are resolved.
- [ ] Authoritative \`qualified_v1\` remains disabled, no live traffic occurred,
  and Ticket 07 is unblocked only by a complete passing evidence package.

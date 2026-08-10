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
acquisition; 05B — Persist and restore indicator metadata compatibility; 06A —
Correct QCF4 deterministic acceptance.

**Status:** COMPLETE

**Acceptance state:** PASS and complete on 2026-08-10. Prerequisites 02E, 02F,
05B, and 06A are complete; corrected QCF4 passed before QCF5–QCF12. This clears
Ticket 07's blocker but does not execute Ticket 07 or authorize rollout.

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

QCF1–QCF18 retain their IDs and authority. QCF1–QCF3 and QCF5–QCF18 retain
their scenarios and pass/fail meaning; QCF4 alone uses the corrected semantics
below. QCF4–QCF12 must originate from a fresh built-in run. A manually seeded
Financial Eligibility Acquisition Context, callable runtime, or checkpoint
cannot satisfy fresh acceptance for any of QCF4–QCF12. QCF5–QCF12 are not
runnable or passing until Ticket 06A passes corrected QCF4.

- [x] **QCF1 — Built-in reachability:** CLI and programmatic explicit validation
  select the statically registered built-in without injection/factory path;
  graph receives the managed plan-bound composition and
  \`qualified_routing_composition_missing\` is absent.
- [x] **QCF2 — Override precedence:** direct composition invokes neither
  factory; explicit operator factory wins over built-in; safe-config and
  plan-signature validation remain exact.
- [x] **QCF3 — Zero-work/authority controls:** legacy, ordinary non-validation
  shadow, empty/non-applicable plan, and \`qualified_v1\` controls construct no
  qualified resource or I/O; authority-disabled preflight precedes built-in
  construction.
- [x] **QCF4 — Truthful complete Tushare metadata:** from the required fresh
  built-in origin, complete fixed Tushare company-type, statement, and indicator
  fixtures plus authoritative BaoStock lifecycle metadata originate and bind
  the context before ordinary dispatch. Compatible Tushare statements are
  retained and selected and stop lower statement fallback. Tushare indicator
  artifacts/candidates and 100% value-completeness assessments are retained;
  ratio/rate/turnover value currency is `NOT_APPLICABLE` where declared by the
  pinned field policy, filing consolidation scope is material and `UNPROVEN`,
  and missing `f_ann_date` remains current-only at most, so affected Tushare
  indicator families remain unselected. Only unresolved indicator families
  continue sequentially through the immutable Capability Routing Plan, and
  lower providers qualify only through their own truthful metadata. Requested
  `CNY`/`CONSOLIDATED` is never copied into provider evidence. Exact prerequisite
  and ordinary physical-attempt cardinality includes actual lower-provider work
  with no hidden call, duplicate bulk work, provider-specific router branch,
  or fan-out.
- [x] **QCF5 — Tushare gaps:** successful artifacts/periods survive unavailable
  endpoints and partial gaps; only unresolved work continues; no successful
  bulk acquisition repeats per target.
- [x] **QCF6 — AKShare-Sina salvage:** AKShare-Sina is called sequentially only
  for unresolved statements, retains usable partial results, and prevents Yahoo
  calls for resolved targets.
- [x] **QCF7 — Yahoo degraded fallback:** Yahoo remains last, coordinated, and
  selected only under existing current-only/degraded contracts; candidate-free
  profile and derived-factor behavior do not acquire strict/native authority.
- [x] **QCF8 — BaoStock authority:** raw/factor/status/lifecycle/ratio fixtures
  cover tradeable and suspended sessions; only BaoStock establishes Current
  Tradeability, strict three-component bundle identity remains atomic, and
  ratios are requested narrowly.
- [x] **QCF9 — Factor/name routes:** factor and issuer-name calls retain distinct
  plan routes, endpoint scopes, keys, artifacts, attempts, and authority while
  sharing one cache; names remain descriptive and bundles are never blended.
- [x] **QCF10 — Exact attempts:** complete, retryable, terminal, cooldown-skip,
  partial-fallback, and restored-cache matrices for every assembled provider
  give one event per real transport call, exact configured retries/fallback,
  zero events for non-physical work, and no hidden retry/fan-out.
- [x] **QCF11 — Single-flight/bulk reuse:** identical concurrent subrequests
  share one physical sequence and terminal result; one bulk artifact serves all
  covered targets/calls with deterministic reuse; materially different keys do
  not collide.
- [x] **QCF12 — Exact restoration:** a plan-1.2 checkpoint produced by the fresh
  path restores context, eligibility, field-semantics and metadata-compatibility
  versions, assessment identities/states, dispatcher/cache, artifacts,
  candidates, typed rejections, selections, attempts, and IDs exactly; repeated
  work performs zero provider/network/model I/O or coordinator permits.
  Missing, old/absent, corrupt, contradictory, or mismatched compatibility state
  fails typed or requires explicit `START_NEW`, without current-registry repair
  or historical rewrite.
- [x] **QCF13 — Pre-open validation:** every missing credential/dependency and
  invalid mode/plan/asset/pacing/Safety Ceiling/account scope/Data Usage
  Mode/runtime root/source coverage/output case fails secret-free before any
  graph/model/provider/store/artifact/report/attempt work.
- [x] **QCF14 — Lifecycle cleanup:** failure after every construction step and
  during graph initialization, dispatch, execution, checkpoint publication, and
  \`KeyboardInterrupt\` preserves the original error; interrupt/close is
  idempotent; all sessions/connections/stores/leases/workers are released or
  terminally abandoned.
- [x] **QCF15 — Security canaries:** tokens, credential-like text, credentialed
  URLs, and raw provider exceptions appear nowhere in config, plan, repr, IDs,
  events, exceptions, logs, reports, checkpoints, artifacts, manifests, audits,
  or terminal output; only existing safe typed diagnostics cross boundaries.
- [x] **QCF16 — Authority invariance:** legacy output, qualified shadow output,
  daily acquisition, Evidence Admission, Decision Gate, report, checkpoint, and
  decision audit comparisons show unchanged legacy/daily authority and
  \`shadow_non_authoritative\` qualified output.
- [x] **QCF17 — Historical immutability:** representative historical material is
  byte-for-byte unchanged/readable under its recorded contracts; plan-1.2 runs
  use the exact forward indicator versions from the amended specification while
  unchanged raw artifact/candidate namespaces remain stable, with no migration,
  reinterpretation, rewrite, deletion, relabeling, or renumbering.
- [x] **QCF18 — Unchanged parent assurance:** PE1–PE15 and parent AC1–AC13 pass
  with their existing text, fixtures, expected results, and authority, followed
  by the full supported suite twice, Ruff, whitespace, and diff/scope checks,
  all without waiver.

## Deterministic regressions

- Run every focused test added by Tickets 01–05, 02A–02F, 04A, 05A, 05B, and
  06A plus existing adapter, coordinator/cache, rollout, checkpoint, graph-
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
- Historical checkpoints retain their recorded-version read behavior and are
  never reinterpreted or upgraded in place. A plan-1.2 restore from an old or
  absent metadata-compatibility checkpoint fails typed or requires explicit
  `START_NEW`, with zero I/O and no repair.
- Capability Routing Plan 1.2 remains the sole routing authority. The unchanged
  completeness gates (100% critical and at least 90% declared-core), financial-
  period eligibility v2, current-only/strict-PIT boundaries,
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
- New router, cache, persistence authority, checkpoint sidecar, provider
  registry, ID namespace, historical migration, provider fan-out, hidden retry,
  account rotation, speculative prewarming, or any schema/version beyond the
  exact forward contracts in the amended specification.
- Waiving or renumbering QCF1–QCF18, PE1–PE15, or parent AC1–AC13 to obtain a
  pass; only QCF4's over-specified expectation is corrected by Ticket 06A.

## Verification commands

Run production-composition and provider focused suites:

\`\`\`powershell
pytest -q tests/test_qualified_routing_composition_contract.py tests/test_production_qualified_provider_composition.py tests/test_qualified_routing_static_registration.py tests/test_qualified_routing_managed_lifecycle.py tests/test_qualified_routing_factory_checkpoint_restore.py
pytest -q tests/test_financial_eligibility_acquisition_context.py tests/test_financial_eligibility_metadata_acquisition.py tests/test_financial_request_factory_context_binding.py
pytest -q tests/test_qualified_production_source_bindings.py tests/test_qualified_routing_eligibility_context_lifecycle.py tests/test_qualified_routing_checkpoint_context_restore.py
pytest -q tests/test_financial_indicator_field_semantics.py tests/test_financial_indicator_metadata_compatibility.py tests/test_financial_indicator_compatibility_persistence.py
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

## Acceptance evidence — 2026-08-10

All deterministic pytest invocations explicitly set
`RUN_LIVE_DEEPSEEK_TESTS=0`, `RUN_LIVE_TUSHARE_TESTS=0`, and
`RUN_LIVE_PROVIDER_TESTS=0` immediately before pytest. Credentials in the
process environment or `.env` therefore could not opt a test into live work.
No live provider, qualification cache, integration workflow, or paid model ran.

### QCF1–QCF18 traceability

| ID | Named deterministic evidence | Result |
| --- | --- | --- |
| QCF1 | `test_static_builtin_registration_is_immutable_and_deterministic`, `test_cli_and_programmatic_callers_resolve_same_builtin_identity_and_blueprint`, `test_real_graph_accepts_registered_builtin_without_provider_io` | PASS |
| QCF2 | `test_public_builder_precedence_is_direct_then_operator_then_builtin`, invalid-operator/output static-registration cases | PASS |
| QCF3 | `test_non_applicable_paths_do_not_validate_or_invoke_builtin`, `test_authoritative_qualified_plan_is_rejected_before_builtin_invocation`, rollout disabled/legacy cases | PASS |
| QCF4 | `test_qcf4_only_unresolved_indicator_family_continues_fallback`; `test_qcf4_fresh_builtin_retains_incompatible_tushare_and_falls_back` for 601328.SS and 600895.SS in both input orders | PASS: 5 |
| QCF5 | `test_qcf5_fresh_builtin_preserves_partial_tushare_work_and_gaps` | PASS |
| QCF6 | `test_qcf6_fresh_builtin_akshare_salvages_only_unresolved_statement` | PASS |
| QCF7 | `test_qcf7_fresh_builtin_yahoo_profile_and_derived_factor_stay_degraded` | PASS |
| QCF8 | `test_qcf8_fresh_builtin_baostock_owns_strict_bundle_and_tradeability` for tradeable and suspended sessions | PASS: 2 |
| QCF9 | Four `test_qcf9_fresh_builtin_*` cases covering distinct AKShare, Tushare, BaoStock, and Yahoo-derived factor/name outcomes | PASS: 4 |
| QCF10 | `test_qcf10_fresh_builtin_exact_terminal_attempt_and_cache_hits` plus coordinator/cache retry matrices | PASS |
| QCF11 | `test_qcf11_fresh_builtin_single_flight_and_bulk_reuse` plus provider single-flight suites | PASS |
| QCF12 | `test_qcf12_fresh_create_checkpoint_restores_with_zero_io` plus factory/context checkpoint suites | PASS |
| QCF13 | `test_missing_duplicate_and_unknown_source_coverage_fail_before_factories`, `test_explicit_validation_preflight_failure_stops_before_run_artifacts`, contract/pre-open matrices | PASS |
| QCF14 | `test_partial_construction_failure_closes_only_created_resources_in_reverse`, acquisition/graph/dispatch/publication `BaseException` cases, concurrent idempotent close/interrupt cases | PASS |
| QCF15 | `test_blueprint_public_values_are_secret_payload_url_and_path_free`, `test_external_source_raw_exception_is_sanitized_at_the_composition_seam`, `test_token_like_and_raw_operational_values_are_rejected_from_contracts` | PASS |
| QCF16 | `test_default_rollout_remains_byte_compatible_legacy`, `test_shadow_plan_is_explicit_qualified_routing_but_non_authoritative`, disabled-authority and daily-route compatibility cases | PASS |
| QCF17 | `test_plan_1_2_indicator_contracts_write_forward_versions_and_round_trip`, `test_ticket06_legacy_audit_evidence_bytes_omit_v2_binding`, historical rollback/readability cases | PASS |
| QCF18 | PE1–PE15 and AC1–AC13 matrices below, non-integration, two complete-suite passes, Ruff, and diff checks | PASS |

The corrected QCF4 gate ran before QCF5–QCF12 and passed `5 passed, 14
deselected`. The gated QCF5–QCF12 selector then passed `12 passed, 7
deselected`; the complete acceptance file passed `19 passed`. Both fixed-symbol
QCF4 paths began with a new static built-in composition, an unbound owner, no
callable financial runtime, no seeded eligibility context, and no checkpoint.
Each resolved company type once and bound authoritative BaoStock listing
provenance before dispatch. Complete Tushare statements stopped statement
fallback. Complete Tushare indicator values remained retained but unselected
where material filing scope was `UNPROVEN`; pinned ratio/rate/turnover field
semantics alone supplied `NOT_APPLICABLE` value currency. Requested
`CNY`/`CONSOLIDATED` never appeared as provider evidence, and only unresolved
families continued through plan order.

QCF4 recorded exactly 54 unique physical-attempt events per fixture: two
eligibility prerequisites, one coordinated Tushare statement acquisition, and
51 indicator-route attempts (Tushare 1, AKShare 1, BaoStock qualified families
48, Yahoo 1). Every event had `attempt_index=1` and
`final_physical_attempt_count=1`; there was no hidden retry, fan-out, or
duplicate bulk acquisition. BaoStock ratio work was exactly eight narrow calls
to each of its six ratio-family endpoints. QCF10 proved one real rate-limited
AKShare invocation equals one attempt, a distinct cooldown-skipped request
equals zero attempts, and a terminal cache hit adds zero calls/attempts. QCF11
proved one metadata acquisition, one company-type resolution, one BaoStock
lifecycle acquisition, one coordinated statement bulk attempt reused by two
first consumers, and one factor invocation/attempt shared by concurrent
callers.

QCF12 executed fresh graph creation → fresh eligibility context → terminal
qualified shadow work → MemorySaver checkpoint publication → resource
close → reconstruction through the shipped built-in factory → exact local
restore → repeated request. The repeat preserved the exact ledger, cache,
routing result, attempt IDs, artifact bytes, run ID, and context while recording
zero coordinator permits, artifact writes, metadata acquisitions, company-type
resolutions, provider/network calls, new attempts, or model calls. Old, absent,
corrupt, contradictory, and mismatched variants derived from the real
checkpoint all failed typed before I/O and without repair.

All six executable production-source replacements were exercised:

| Production source | Deterministic evidence | Result |
| --- | --- | --- |
| Yahoo statements | QCF5 unresolved statement fallback and production source-binding suite | PASS |
| AKShare indicators | Corrected QCF4 unresolved indicator continuation | PASS |
| Yahoo current profile | QCF7 candidate-free/non-authoritative profile | PASS |
| AKShare factors | QCF9 distinct factor/name fallback | PASS |
| Yahoo-derived factors | QCF7 degraded behavior and QCF9 terminal fallback | PASS |
| AKShare issuer-name events | QCF9 distinct key/artifact/attempt/authority | PASS |

### Parent PE1–PE15 traceability

| ID | Named deterministic evidence | Result |
| --- | --- | --- |
| PE1 | `test_2026_08_01_targets_are_publication_aware_and_exact` | PASS |
| PE2 | `test_announcement_cutoff_is_inclusive_and_advances_only_reporting_frontier` | PASS |
| PE3 | `test_complete_announced_indicators_are_explicitly_current_only_and_degraded` | PASS |
| PE4 | `test_strict_pit_rejects_current_only_candidates_with_exact_reason` | PASS |
| PE5 | exact five-annual/eight-reporting assertions in `test_2026_08_01_targets_are_publication_aware_and_exact` | PASS |
| PE6 | `test_authoritative_since_listing_exception_reduces_only_pre_listing_ends`, `test_missing_post_listing_publication_fails_the_exception` | PASS |
| PE7 | 601328 bank branch in `test_ticket07_corrected_shadow_joint_acceptance` and sparse-family adapter cases | PASS |
| PE8 | 600895 non-bank branch in `test_ticket07_corrected_shadow_joint_acceptance` and complete-family adapter cases | PASS |
| PE9 | `test_equivalent_later_route_is_visible_without_coverage_or_source_fact_inflation` | PASS |
| PE10 | `test_critical_conflict_is_period_local_in_audit_and_evidence` | PASS |
| PE11 | `test_update_flag_alone_creates_no_ordering_or_supersession_claim` | PASS |
| PE12 | `test_pre_v2_qualified_checkpoint_cannot_resume_as_v2_shadow` | PASS |
| PE13 | `test_shadow_plan_is_explicit_qualified_routing_but_non_authoritative` | PASS |
| PE14 | `test_v2_checkpoint_round_trip_restores_without_io_and_rejects_tampering`, `test_v2_audit_and_report_expose_safe_deterministic_completeness` | PASS |
| PE15 | Unchanged AC1–AC13 matrix below | PASS |

The authoritative PE focused commands passed `143 passed` and `242 passed, 11
subtests passed`. The joint corrected-shadow fixture passed inside the
`206 passed` graph/shadow group and both full suites.

### Parent AC1–AC13 traceability

| ID | Named deterministic evidence | Result |
| --- | --- | --- |
| AC1 | `test_current_mainland_suspension_blocks_direction_with_dedicated_outcome` | PASS |
| AC2 | `test_authoritative_snapshot_preserves_tradeability_in_canonical_evidence`, `test_authoritative_traded_baostock_status_remains_tradeable` | PASS |
| AC3 | `test_snapshot_v2_identity_changes_for_status_only_revision`, exact-pin conflict/membership tests | PASS |
| AC4 | `test_incremental_history_refresh_preserves_complete_bundle_and_calendar`, `test_incremental_bundle_audit_identifies_exact_retained_and_refreshed_members` | PASS |
| AC5 | strict replay correction/backfill tests, including `test_strict_replay_admits_a_correction_only_at_its_observation_time` | PASS |
| AC6 | complete `test_market_history_payload_gc_concurrency.py` matrix, unlink/timeout/publication tests | PASS |
| AC7 | `test_yahoo_backed_sol_cli_and_programmatic_contracts_match_full_asset_semantics` | PASS |
| AC8 | `test_crypto_registry_refresh_rejects_unsupported_member_before_publication`, `test_cli_unsupported_crypto_symbol_uses_typed_crypto_policy` | PASS |
| AC9 | `test_yahoo_retry_sequence_persists_one_typed_event_per_physical_attempt` | PASS |
| AC10 | exact budget and 429 cooldown tests in provider coordinator/Yahoo retry suites | PASS |
| AC11 | `test_valid_legacy_v1_pin_remains_readable_and_incomplete_pin_fails_closed` plus legacy migration/readability matrix | PASS |
| AC12 | `test_default_runtime_market_history_is_gitignored_without_hiding_other_data`, `test_default_runtime_cli_smoke_preserves_clean_worktree` | PASS |
| AC13 | focused groups, non-integration, two full suites, Ruff, and diff validation | PASS |

The unchanged parent focused groups passed 131, 118, 124, and 113 tests. The
market-history/gitignore selector passed `2 passed, 2944 deselected`; the
clean-worktree/default-runtime selector passed `3 passed, 2943 deselected` and
Git status was identical before/after (6 entries each, delta 0).

### Command and review ledger

- Production composition/contract/static registration/lifecycle/checkpoint:
  `158 passed`.
- Eligibility acquisition/context/request-factory binding: `88 passed`.
- Six production-source bindings, eligibility lifecycle, context restore:
  `75 passed`.
- Indicator field semantics/metadata compatibility/persistence: `63 passed`.
- Tushare, AKShare-Sina, and BaoStock adapter matrix: `189 passed`.
- Subrequest cache/coordinator/attempt/Yahoo retry matrix: `137 passed`.
- Graph/dispatcher/shadow matrix: `206 passed`.
- Checkpoint/report/audit/Evidence Admission matrix: `171 passed, 11 subtests`.
- Market/BaoStock authority matrix: `146 passed` (one existing pandas warning).
- Live-test isolation: `9 passed`.
- Non-integration: `2939 passed, 7 deselected, 82 subtests`, 19 known warnings.
- Final complete-suite pass A: `2944 passed, 2 skipped, 82 subtests`, 19 known
  warnings. The skips explicitly required live DeepSeek/Tushare opt-ins.
- Final complete-suite pass B: identical counts and skip reasons.
- Ruff: `ruff check tradingagents cli tests` passed.
- Whitespace: `git diff --check` passed.

One intermediate complete-suite attempt observed a scheduler-sensitive timing
assertion in `test_flush_surfaces_background_failure_without_waiting_for_timeout`
(0.817 s versus the existing 0.5 s bound). The exact test then passed 20 serial
and 8 concurrent stress repetitions without changing its timeout or code, and
the two final complete-suite passes succeeded from the unchanged tree.

Stage 1 Spec Compliance review: PASS; Critical 0, Important 0, Minor 0. Stage 2
Code Quality review: PASS; Critical 0, Important 0, Minor 0. Stage 2 explicitly
reviewed fresh-origin integrity, metadata truthfulness, attempt cardinality,
zero-I/O restore, lifecycle cleanup, shadow authority, deterministic test
quality, secret safety, and unrelated scope.

Normal completion, partial construction failure, context acquisition/binding
failure, graph failure, checkpoint/report failure, `KeyboardInterrupt`,
`SystemExit`, repeated interrupt/close, and cleanup-failure paths all released or
terminally abandoned owned sessions, workers, leases, stores, caches, providers,
and connections without masking the primary exception. Security suites found
no token, credential, payload body, raw provider/model exception, URL query,
machine-local absolute path, or disallowed `raw_text` crossing safe boundaries.

Legacy authority and bytes remain unchanged; qualified output remains
`shadow_non_authoritative`; the daily mainland route remains AKShare → BaoStock
→ Yahoo; BaoStock remains Current Tradeability and strict-history authority;
Evidence Admission and Decision Gate remain unchanged. Authoritative
`qualified_v1` remains disabled. Ticket 07 was neither implemented nor run; its
blocker is cleared only by this completed evidence package.

## Definition of done

- [x] A durable traceability record maps QCF1–QCF18, PE1–PE15, and parent
  AC1–AC13 to named deterministic tests, commands, evidence, and passing results.
- [x] Focused suites, non-integration suite, full suite twice, Ruff, whitespace,
  secret/payload, historical-immutability, and scope checks all pass without
  waiver.
- [x] **Stage 1 — Spec Compliance Review:** confirms the exact production
  contract, routes, eligibility, ownership, restoration, authority,
  compatibility, acceptance IDs, blocking edges, and every explicit non-goal.
  Any functional gap is resolved before Stage 2.
- [x] **Stage 2 — Code Quality Review:** after Stage 1 passes, confirms error
  handling, lifecycle durability, concurrency, typed/sanitized boundaries,
  deterministic tests, reuse of existing seams, maintainability, and absence of
  unrelated changes. All Critical and Important findings are resolved.
- [x] Authoritative \`qualified_v1\` remains disabled, no live traffic occurred,
  and Ticket 07 is unblocked only by a complete passing evidence package.

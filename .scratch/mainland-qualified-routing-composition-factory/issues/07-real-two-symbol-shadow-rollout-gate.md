# 07 — Run the authorized real two-symbol shadow rollout gate

**Parent specification:** [Mainland qualified routing production composition factory](../spec.md)

**What to build:** Only after final Ticket 06 PASS through ISO-04, obtain a
separate fresh Ticket 07 live authorization and run the shipped built-in
production composition through the real CLI validation seam for `601328.SS`
first and `600895.SS` only after the new bank run passes, one at a time for the
same latest completed mainland session. Retain complete safe evidence showing
exact/non-duplicated provider attempts, BaoStock Current Tradeability authority,
report and decision-audit publication, `shadow_non_authoritative` qualified
output, unchanged authoritative legacy output, and still-disabled
`qualified_v1`.

**Blocked by:** Final Ticket 06 PASS after ISO-04. After that deterministic
blocker clears, Ticket 07 still requires a separate fresh live authorization
before allocating a NEW run ID or opening provider/model resources.

**Status:** blocked

**Gate state:** BLOCKED. CAL-01 through CAL-04, production mainland calendar
bootstrap, and exact-ID readiness are complete and persisted. Final Ticket 06
is otherwise green but cannot PASS until ISO-04 establishes conjunctive QCF17
and conditional QCF18. The historical failed `601328.SS` run and the 2026-08-12
calendar-missing blocked attempt remain immutable. No fresh Ticket 07
authorization or NEW run/checkpoint namespace exists, no provider/model I/O is
authorized, and authoritative `qualified_v1` remains disabled.

## Exact dependency graph

```text
CAL-01 -> CAL-02 -> CAL-03 -> CAL-04 [complete]
CAL-04 -> production calendar bootstrap [complete]
production calendar readiness -> ISO-01
ISO-01 -> ISO-02 -> ISO-03
ISO-03 PASS -> freeze new additive post-incident host baseline
baseline freeze -> ISO-04
ISO-04 -> Ticket 06 PASS
Ticket 06 PASS -> fresh Ticket 07 authorization
fresh Ticket 07 authorization -> NEW 601328.SS
NEW 601328.SS PASS -> NEW 600895.SS
```

## Observable behavior

- The completed production calendar bootstrap and exact-ID readiness remain
  persisted prerequisite evidence. Their earlier authorization is not Ticket 07
  authorization and cannot be reused for symbol/provider/model work.
- Before any Ticket 07 run/checkpoint identity, provider/model runtime, or
  symbol-specific work, preflight reopens the exact persisted calendar revision
  and proves complete range, authority/session-time contracts, latest completed
  session and close, later open session and close, and exact identity/content
  with zero provider I/O. Missing, stale, incomplete, mismatched, or unreadable
  state keeps this ticket blocked.
- The operator confirms final Ticket 06 PASS after ISO-04 on the exact reviewed
  source/bundle/runner/config/protected-manifest/baseline identities, including
  QCF17 zero host delta and conditional QCF18 PASS. Only then may the operator
  grant a separate fresh Ticket 07 authorization under the configured Data
  Usage Mode and provider terms. Without that distinct authorization, stop
  without allocating a run ID or opening provider/model resources.
- Both runs use the shipped static built-in selection with no directly injected
  composition, no \`MODULE:CALLABLE\` operator factory, no test module, and no
  dynamic-import fixture.
- A newly allocated run ID and checkpoint namespace are mandatory for each
  attempt. The failed `601328.SS` run for requested session `2026-08-10`, its
  artifact retrieved at `2026-08-10T16:48:07Z`, and every historical report,
  checkpoint, attempt, artifact, decision, audit, snapshot, pin, provider row,
  and ID remain immutable and cannot be resumed, repaired, relabeled, or reused.
- Each run originates and binds one immutable Financial Eligibility Acquisition
  Context without a manually seeded context, runtime, or checkpoint, and retains
  its safe version, identity, and projection before ordinary financial dispatch.
- Run \`601328.SS\` first as the bank Equity. Start \`600895.SS\` as the non-bank
  Equity only after the new bank run passes every gate; a bank failure ends the
  retry without calling the second symbol. Both target the same latest completed
  mainland session.
- Each run retains its safe preflight result, Capability Routing Plan signature,
  plan-1.3/context-v2 temporal and indicator contract bindings, pinned calendar
  revision/binding, fixed evaluation instant, provider-derived effective/
  publication proof, temporal decision identities/results/reasons, metadata-
  compatibility assessment identities/states, cache-1.2/ledger-1.6 checkpoint
  projection, manifest v4, provider physical-attempt events, complete report,
  and decision audit.
- Both runs avoid \`qualified_routing_composition_missing\`, complete through
  the registered composition, coordinate every physical call, establish
  BaoStock Current Tradeability authority, and publish qualified observations
  as \`shadow_non_authoritative\` while legacy-selected output remains
  authoritative.
- Complete Tushare indicator artifacts remain retained even when material
  metadata is incompatible; only unresolved families continue through the
  Capability Routing Plan, and each lower provider is judged only by its own
  truthful metadata.
- Fresh and retained company-type/listing evidence must independently pass the
  one temporal evaluator. Request/session/retrieval dates cannot manufacture
  provider proof, and restore cannot consult today's calendar frontier.

## Acceptance criteria

- [x] CAL-01–CAL-04 are complete, and the production calendar bootstrap had its
  own explicit authorization and safe receipt. That authorization performs no
  symbol, analysis run/checkpoint, Ticket 07, or model work.
- [x] The exact returned `mainland-cn` revision is persisted and has passed
  structural readiness with zero provider clients, permits, logical requests,
  transports, attempt events, or writes at read time.
- [ ] ISO-04 completes on the exact reviewed source, bundle, runner/config,
  protected-manifest, and frozen-baseline identities, and final Ticket 06 is
  PASS with QCF17 sandbox acceptance plus zero trusted host delta and QCF18
  credited only after QCF17 PASS.
- [ ] A separate fresh Ticket 07 operator authorization, Data Usage Mode,
  credential boundary,
  Operator Safety Ceilings, pacing, exact plan signature, and latest completed
  mainland session are recorded safely without copying credentials.
- [ ] Authoritative \`qualified_v1\` is verified disabled immediately before
  the first run.
- [ ] \`601328.SS\` completes first through the real CLI
  \`qualified_v1_shadow\` validation seam using only the shipped built-in
  composition in a new run/checkpoint namespace; its report and decision audit
  are complete and every gate passes.
- [ ] \`600895.SS\` starts only after the bank run passes and completes
  through the same seam and policy inputs in its own new run/checkpoint
  namespace; its report and decision audit are complete and retained.
- [ ] Each symbol's fresh built-in run acquires company type and authoritative
  BaoStock listing provenance, binds exactly one immutable eligibility context
  before factory/dispatcher exposure, and retains its safe context identity and
  projection with no manual seed.
- [ ] Each context uses plan 1.3/context v2, one fixed
  `eligibility_evaluated_at`, a pinned `market-session-eligibility-binding-v1`,
  and exact provider-derived `evidence_effective_as_of`, `ann_date`, applicable
  `f_ann_date`, immutable `observed_at`/`retrieved_at`, latest-completed-session
  decision, and temporal decision identities/results/reasons. Missing proof
  fails the gate without request/retrieval-date backfill.
- [ ] For both runs, every transport call has exactly one existing-schema
  physical-attempt event; retries/fallback match configured budgets and plan
  order; cache hits, reuse, skips, selections, and derived facts add zero
  attempts; no hidden retry, duplicate bulk call, fan-out, or uncoordinated
  request appears.
- [ ] For both runs, BaoStock is the authority for Current Tradeability.
  Tushare names and supplemental sources establish no suspension, ST,
  lifecycle, or tradeability authority; strict Provider History Bundles are not
  blended.
- [ ] For both runs, plan 1.3 preserves and pins
  `financial-indicator-field-semantics-v1` and
  `financial-indicator-metadata-compatibility-v1`; complete-but-incompatible
  indicator candidates remain retained, material `UNPROVEN` metadata prevents
  selection, and `NOT_APPLICABLE` is ignored only under the pinned field policy.
- [ ] Requested `CNY`/`CONSOLIDATED` never appears as provider-origin evidence;
  only unresolved indicator families continue through plan-owned fallback and
  lower providers are assessed solely from their own metadata.
- [ ] Reports, manifests, checkpoints, decision audits, and terminal output say
  \`shadow_non_authoritative\`; qualified results do not replace legacy-selected
  tool output, admitted Source Facts, Validated Decision Context, or the final
  Trading Decision.
- [ ] Tokens, credential-bearing URLs, raw provider exception text, and unsafe
  payloads are absent from terminal output and all retained public/persisted
  metadata outside existing immutable provider-artifact boundaries.
- [ ] Authoritative \`qualified_v1\` is verified disabled again after both runs.
  Passing produces rollout evidence for a separate future activation decision,
  not activation or a recommendation to activate.

## Deterministic regressions

- Before live work, verify the completed CAL-04 and exact-ID zero-I/O readiness
  evidence plus final Ticket 06 PASS from ISO-04, including its exact identities,
  bounded export, conjunctive QCF17 result, conditional QCF18 result, and zero
  trusted host delta. Any missing or failed evidence stops the rollout before
  run-ID allocation.
- Reconfirm exact zero-I/O replay for every temporal decision class and the
  32-follower one-evaluation control without consulting the live calendar.
- Capture pre/post hashes or immutable references for representative historical
  material and prove the two runs do not rewrite it.
- Audit exact provider-attempt accounting against the immutable plan separately
  for each symbol, including retries, skips, cache reuse, and sequential
  fallback.
- Compare report/audit authority fields, legacy-selected outputs, BaoStock
  status provenance, and \`qualified_v1\` disabled preflight before/after.
- If a run fails, preserve it unchanged. A retry requires a new run identity and
  repeats the preflight/accounting checks from `601328.SS`; `600895.SS` remains
  uncalled until that new bank run passes.

## Compatibility and migration behavior

- Each live run is additive and uses a fresh namespace with the exact plan-1.3/
  context-v2 temporal versions, preserved plan-1.2 indicator semantic bindings,
  unchanged raw artifact/candidate identities, and
  existing coordinator, cache, artifact store, dispatcher, checkpoint, graph,
  report, and audit seams.
- Historical state remains immutable and readable. No cleanup, migration,
  relabeling, ID renumbering, checkpoint upgrade, or failed-run rewrite is
  permitted.
- Plan-1.2/context-v1 state under plan 1.3 fails typed or requires explicit
  `START_NEW`; the retry never consults today's calendar frontier to reinterpret
  the failed historical run.
- Existing provider routes, completeness thresholds, financial eligibility,
  Evidence Admission, Decision Gate, BaoStock authority, daily AKShare →
  BaoStock → Yahoo routing, and legacy authority remain unchanged.
- A typed provider/configuration failure is a gate failure, not permission to
  change policy or repair historical state live.

## Explicit non-goals

- Running Ticket 07 before ISO-04/final Ticket 06 PASS or without a separate
  fresh live authorization.
- Enabling or recommending authoritative \`qualified_v1\`, changing its
  activation gate, or treating a shadow pass as an authority decision.
- Direct composition injection, explicit operator factory, test factory,
  dynamic import for the built-in, provider qualification, route experimentation,
  threshold/eligibility changes, or alternative provider order.
- Clearing historical data, reusing a failed run identity, rewriting a failed
  run, live checkpoint/artifact repair, hidden retries, provider fan-out,
  account rotation, or speculative prewarming.
- Copying/importing temporal prototype implementation, local-date or grace-window
  substitutions, request/retrieval-date proof backfill, or enabling
  authoritative `qualified_v1`.
- Code changes during rollout unless a reproducible runtime defect is confirmed
  against deterministic evidence. Policy/data-quality/provider-availability
  outcomes are not code defects.
- Treating calendar-bootstrap authorization as Ticket 07 authorization, letting
  qualified preflight acquire/refresh/repair/publish a calendar, or executing a
  symbol under the bootstrap authorization.
- Copying/importing the calendar-bootstrap prototype implementation or enabling
  `qualified_v1`.
- Running Ticket 06 slices on the host, weakening QCF17, reusing a QCF17
  baseline, or copying the QCF17 isolation prototype implementation into
  production.

## Verification commands

**Do not run any command in this ticket yet.** Before seeking the separate fresh
Ticket 07 live authorization, first inspect and validate the completed ISO-04
bounded evidence and final Ticket 06 PASS. Then, under that later authorization,
run the deterministic preflight checks below without integration/live opt-ins:

\`\`\`powershell
pytest -q tests/test_baostock_session_calendar_acquisition.py tests/test_market_session_calendar_publication.py tests/test_market_session_calendar_exact_reader.py tests/test_market_session_calendar_readiness.py tests/test_market_session_calendar_bootstrap.py
pytest -q tests/test_financial_metadata_temporal_eligibility.py tests/test_financial_temporal_proof_projection.py tests/test_financial_temporal_checkpoint_compatibility.py
pytest -q tests/test_qualified_routing_composition_acceptance.py
pytest -q tests/test_qualified_routing_composition_contract.py tests/test_production_qualified_provider_composition.py tests/test_qualified_routing_static_registration.py tests/test_qualified_routing_managed_lifecycle.py tests/test_qualified_routing_factory_checkpoint_restore.py
pytest -q tests/test_financial_eligibility_acquisition_context.py tests/test_financial_eligibility_metadata_acquisition.py tests/test_financial_request_factory_context_binding.py
pytest -q tests/test_qualified_production_source_bindings.py tests/test_qualified_routing_eligibility_context_lifecycle.py tests/test_qualified_routing_checkpoint_context_restore.py
pytest -q tests/test_qualified_routing_rollout.py tests/test_corrected_shadow_joint_acceptance.py
pytest -q -m "not integration"
ruff check tradingagents cli tests
git diff --check
git status --short
tradingagents analyze --help
\`\`\`

Only after final Ticket 06 PASS through ISO-04, obtain a separate fresh Ticket
07 authorization. Then revalidate the exact persisted calendar revision with
zero I/O, allocate a NEW run ID, and invoke the following command once per
symbol. On the first invocation
select \`601328.SS\`; after it finishes, allocate a new run/checkpoint namespace,
invoke it again, and select \`600895.SS\`. Do not pass
\`--qualified-routing-composition-factory\` and do not clear checkpoints:

\`\`\`powershell
tradingagents analyze --qualified-routing-validation --checkpoint
tradingagents analyze --qualified-routing-validation --checkpoint
\`\`\`

After both runs, execute the repository's existing safe report, decision-audit,
checkpoint, artifact-reference, and provider-attempt inspection commands for
each new run identity, then:

\`\`\`powershell
pytest -q tests/test_qualified_routing_rollout.py -k "qualified_v1 and disabled"
git diff --check
git status --short
\`\`\`

Do not print environment values or raw provider exceptions while collecting the
gate evidence.

## Definition of done

- [ ] The retained gate record links the completed production-bootstrap receipt,
  exact-ID zero-I/O readiness evidence, final ISO-04/Ticket 06 PASS with QCF17
  zero host delta, and separate fresh Ticket 07 authorization to two sequential,
  uniquely identified CLI runs.
- [ ] Both symbols have complete safe preflight, plan, cache/dispatcher,
  physical-attempt, manifest, report, and decision-audit evidence satisfying
  every acceptance criterion.
- [ ] Exact attempt accounting and BaoStock Current Tradeability authority are
  independently reconciled for each symbol with no unexplained or duplicated
  call.
- [ ] Legacy output remains authoritative, every qualified artifact is
  \`shadow_non_authoritative\`, historical material is untouched, and
  authoritative \`qualified_v1\` remains disabled after the gate.
- [ ] The failed historical bank run remains immutable; every retry has a new
  run ID, begins with `601328.SS`, and reaches `600895.SS` only after the new
  bank case passes.
- [ ] No code changed during rollout, unless a reproducible runtime defect was
  first confirmed and returned to the deterministic implementation/review gate;
  any such change requires Ticket 06 to pass again before retrying live.

## 2026-08-12 authorized retry result

This section is immutable historical evidence for the pre-bootstrap state at
that time. The later CAL chain and production bootstrap resolved that calendar
prerequisite; they do not authorize a new Ticket 07 run and do not alter the
blocked result recorded here.

**Ticket result:** BLOCKED before live execution.

**Classification:** Important — insufficient evidence/configuration. This is
not provider behavior and no concrete production-code defect was established.

The fresh operator authorization was accepted and the post-temporal basis was
confirmed at `21000ab78ec221a6b528b151400a6673007fc947`; formal Ticket 06
closure was confirmed at `f1355714710d751cfc21ba1ee1c4647e25acd26d`, with
the later closure ledger at `b7ff726`. Before allocating a run ID or opening a
provider/model resource, the retry attempted to resolve the latest completed
mainland session from the repository's pinned Market Session Calendar as the
ticket requires. At that time both repository runtime databases contained zero
`market_session_calendars` rows and zero `market_sessions` rows:

That historical authorization ended with the blocked attempt. It does not
authorize production calendar bootstrap or any future Ticket 07 retry; each
requires its own new explicit authorization as stated above.

- `data/market_history/market_history.sqlite3`
- `data/market_history/market_history.sqlite3.provider-requests.sqlite3`

A repository-wide read-only inventory found no other persisted mainland
calendar. The immutable pre-live snapshot created before the historical failed
run independently records the same zero-row calendar state. Consequently there
was no calendar revision to pin and no ADR-0036-authoritative
`latest_completed_session` to use. Wall-clock date, weekday inference, the old
`2026-08-10` requested session, local midnight, next-session open, and price
rows were not substituted for the missing calendar authority.

Ticket 07 says a typed provider/configuration failure is a gate failure rather
than permission to change policy or repair live state. It also forbids another
provider qualification run and requires the session to be resolved before the
first live call. Therefore no separate BaoStock calendar acquisition was added,
no route/fallback or temporal policy was changed, and the CLI live command was
not invoked.

### Safe non-secret preflight

The shipped `.venv` interpreter passed the public, resource-free production
builder preflight without direct composition injection, an operator
`MODULE:CALLABLE`, a test factory, or dynamic import:

| Binding | Observed result |
| --- | --- |
| Selection | static built-in |
| Factory | `production-qualified-routing-composition-factory-v1` |
| Blueprint | valid and plan-bound |
| Managed runtime | not bound; zero provider resource opened |
| Capability Routing Plan | `1.3` |
| Plan signature | `mainland-routing-plan:v1:63be9db56f605cee39a81f54b351e185d67d9aa2ac6bf8bf4bbfc4408ef60ded` |
| Disposition | `shadow_non_authoritative` |
| Data Usage Mode | `personal_research` |
| Validation workload | `true` |
| Intended checkpoint mode | enabled; no namespace allocated |
| Enabled Tushare capabilities | `statements`, `financial_indicators`, `adjustment_factors`, `name_events` |
| Tushare `suspension_status` | disabled |
| Qualification profile | `cn-a-2000-20260729-v1` |
| Account scope label | `personal-research-default` |
| Tushare pacing | 40 calls/minute |
| Tushare Operator Safety Ceiling | 40 calls/minute |
| Yahoo physical-attempt budget | 4 per coordinated sequence |
| Daily market route | AKShare -> BaoStock -> Yahoo |
| Statement routes | Tushare -> AKShare-Sina -> Yahoo |
| Indicator route | Tushare -> AKShare -> BaoStock qualified families -> Yahoo |
| Adjustment-factor route | BaoStock -> Tushare -> AKShare -> Yahoo-derived |
| Name-event route | Tushare -> AKShare |
| Current Tradeability / lifecycle route | BaoStock only |
| Default mode | `legacy` |
| `qualified_v1_authority_enabled` | `false` |

Credential presence was checked only as a boolean through the normal loaded
environment boundary. No credential value was printed, copied, hashed,
persisted, or included in an identity.

No analysis date, model client, analyst roster, model IDs, debate/risk rounds,
or model retry budget was materialized because the run stopped before the CLI
questionnaire and run-config boundary. Those values therefore have no Ticket 07
execution value and are not represented as if a run had used them.

### Live-run and gate outcomes

| Item | Outcome |
| --- | --- |
| Latest completed mainland session | unresolved; no pinned calendar revision exists |
| `601328.SS` run ID | not allocated |
| `601328.SS` terminal outcome | not started; pre-live Gate B/C evidence failure |
| `600895.SS` run ID | not allocated |
| `600895.SS` terminal outcome | not started because the bank gate did not pass |
| New checkpoint namespace | none |
| Complete report / decision audit paths | none; no run was created |
| Statements provider matrix | not produced; zero live requests |
| Six-family indicator matrix | not produced; zero live requests |
| BaoStock tradeability/listing evidence | not produced; zero live requests |
| Factor/name evidence | not produced; zero live requests |
| Checkpoint/resume evidence | not applicable; no new checkpoint or retry |
| Legacy-vs-shadow comparison | not produced; no qualified shadow result existed |

Exact physical-attempt accounting for this authorized retry is:

| Provider / capability | Logical requests | Physical transport invocations | Attempt events | Cache hits | Restored hits | Retries | Fallback calls |
| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| All providers / all capabilities | 0 | 0 | 0 | 0 | 0 | 0 | 0 |

The static/preflight portions of Gate A and all of Gates K, L, and M have safe
pre-live evidence. Gate A's managed-runtime binding and run-bound execution
requirements are unevaluated. Gates B and C are BLOCKED by the absent pinned
calendar and session frontier. Gates D-J cannot be evaluated without weakening
the gate because no live run exists. The overall Ticket 07 result is therefore
BLOCKED, not a partial pass.

### Historical immutability and security

Before and after deterministic verification, the historical failed run
`601328.SS / 20260810_094733` retained four files, 15,982 bytes, and safe
inventory digest
`5f0641dfe66f5a5a7bc7d53ce5354a8919fa5dba8e57a95d6ee2a82aee353aa4`.
Its checkpoint thread `21e972024a446846` retained seven checkpoint rows with
safe digest
`3cc683b0bfc04bc5bae1f2b23657e57c13858f4967b9033f77da383508bfaa6a`
and 54 write rows with safe digest
`ab2dab653df8c0de682793095abe23f698c2ce0e9864327e8e4db87542b70b12`.
No historical file, checkpoint row, run ID, report, attempt, or artifact was
rewritten or reused.

Because no new live run or retained live surface was created, there are no new
`run_status.json`, metrics, message/tool log, report, decision audit, manifest,
checkpoint projection, provider artifact, exception, payload, URL, path, or
`raw_text` surfaces to scan. The resource-free preflight output contained only
safe identities, route names, versions, and booleans. Zero credential values,
token digests, raw provider/model exceptions, URL query strings, provider
payload bodies, machine-local absolute paths, or unsafe raw text were exposed.

### Deterministic verification

Every pytest command explicitly set `RUN_LIVE_DEEPSEEK_TESTS=0`,
`RUN_LIVE_TUSHARE_TESTS=0`, and `RUN_LIVE_PROVIDER_TESTS=0` before collection.
No live pytest integration test was invoked.

| Command/evidence | Result |
| --- | --- |
| Temporal contract, proof projection, and checkpoint acceptance | `62 passed` |
| Complete Ticket 06/QCF acceptance | `19 passed` |
| Ticket 07 rollout/disabled-authority suite | `87 passed` |
| Live-test isolation | `9 passed` |
| PE1-PE15 current authoritative period/acceptance files | `75 passed` |
| AC1-AC13 focused parent assurance group | `373 passed` |
| `pytest -q -m "not integration"` | `3038 passed` |
| `pytest -q` | `3043 passed, 2 skipped, 0 failed` |
| `ruff check tradingagents cli tests` | PASS |
| `git diff --check` | PASS |

The obsolete documented combined PE command names a removed
`tests/test_financial_period_eligibility.py` and collected no tests. The current
authoritative files named by the closure ledger were run directly instead:
`test_financial_period_targets.py`, `test_financial_period_availability.py`,
`test_financial_period_contracts.py`, and
`test_corrected_shadow_joint_acceptance.py`.

No production or test code changed, so there was no authorized defect fix or
TDD cycle. The only scoped tracked change is this final Ticket 07 status and
evidence record. Existing unrelated worktree state was preserved and excluded.

### Remaining blocker and recommendation

CAL-01 → CAL-02 → CAL-03 → CAL-04, the separately authorized production
calendar bootstrap, and exact-ID readiness are now complete and persisted. The
remaining deterministic blocker is ISO-01 → ISO-02 → ISO-03 → additive
post-incident baseline freeze → ISO-04 → final Ticket 06 PASS. Only after that
PASS may an operator grant a separate fresh Ticket 07 authorization. That retry
must allocate a NEW run ID, begin again with `601328.SS`, and use a separate NEW
run/checkpoint identity for `600895.SS` only if the bank case passes. All
previous failed Ticket 07 and QCF17 evidence remains immutable.

The project is **not ready** for a separate authoritative-activation review.
`qualified_v1` remains disabled and the default remains `legacy`.

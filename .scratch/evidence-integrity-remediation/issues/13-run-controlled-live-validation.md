# Run controlled live-model validation

Status: completed

After explicit cost approval, run one equity, one mainland fund, and one optional-source-degraded analysis with bounded model/tool budgets. Compare terminal contracts, rule-backed assertions, retries, latency, token/cost telemetry, and reports against the deterministic acceptance contract.

## Deterministic release gate

Verified on 2026-07-23 without model or provider calls:

- `pytest -q`: 1,392 passed, 2 skipped, 71 subtests passed.
- `ruff check .`: passed.
- `git diff --check`: passed.
- The digest-pinned production Instrument Identity registry matches
  `a40567476d13fea6aecda9fbbf23d39ad39631fed87918a26cae9db251ff4572`.
- `start_tradingagents.ps1 -DryRun` resolves repository launches to:
  - `logs`
  - `data/cache`
  - `data/memory/trading_memory.md`
- Recorded replay, acceptance-matrix mapping, checkpoint binding, telemetry,
  launcher, and dependency-floor tests are included in the passing suite.

The live gate remains separate because the deterministic suite cannot authorize
paid/networked execution or assess stochastic Advisory Commentary.

## Human authorization record

Completed before live-model execution:

- [x] Approver: Judith Sholler
- [x] Approval timestamp: 2026-07-23T22:51:05-07:00
- [x] Approved LLM provider: `deepseek`
- [x] Approved quick model: `deepseek-v4-flash`
- [x] Approved deep model: `deepseek-v4-pro`
- [x] Maximum total spend for all three runs: CNY 33 (USD 5.00 alternative
      authorized; CNY accounting selected because the provider reports CNY)
- [x] Provider-side prepaid balance confirmed: CNY 42.88 before the first run.
      The account balance bounds worst-case exposure; the lower approved CNY 33
      cumulative balance delta remains the validation stop threshold and is
      checked after every sequential run.
- [x] Maximum elapsed time per run: 20 minutes
- [x] Analysis date shared by the three runs: 2026-07-23
- [x] Equity symbol: `600895.SS`
- [x] Mainland Fund symbol: `510500.SS`
- [x] Optional-source-degraded symbol: `600895.SS`
- [x] Reversible mechanism that makes only the named optional capability
      unavailable: for the third run only, apply a process-local
      `tool_vendors["get_news"] = "validation_unavailable"` override. The
      acquisition router records a typed `not_configured` outcome, and process
      exit restores normal configuration.
- [x] Optional capability expected to be unavailable:
      `instrument_news` (`sentiment_news` acquisition telemetry)
- [x] One additional degraded-news attempt authorized by the user at
      2026-07-24T04:12:53-07:00 after the first attempt's external HTTP 503.
      The same fixed envelope applies, with no further automatic retry.

Do not infer approval from an API key, prior run, or general instruction to
continue remediation. Do not use a live-model rating or prose match as an
acceptance oracle.

## Fixed run envelope

Use the same approved provider, quick/deep models, output language, analysis
date, and shallow research topology for all comparable runs.

In the PowerShell process used to launch the runs:

```powershell
$env:TRADINGAGENTS_MAX_DEBATE_ROUNDS = "1"
$env:TRADINGAGENTS_MAX_RISK_ROUNDS = "1"
$env:TRADINGAGENTS_LLM_MAX_RETRIES = "0"
$env:TRADINGAGENTS_CHECKPOINT_ENABLED = "false"
```

Also set the approved provider/model variables explicitly. Keep secrets out of
the issue, logs, command history, and screenshots.

The shallow `debate=1|risk=1` graph and zero SDK retries bound the configured
topology and retry amplification. They are not a hard token or dollar limiter.
The provider-side spending limit or prepaid balance is therefore mandatory.
Run one case at a time and stop before starting the next case if the prior run
exceeds any approved call, token, elapsed-time, or spend threshold.

Use `--no-checkpoint` for each run so the validation cannot resume an older
graph state. Do not delete historical checkpoints for this validation.

## Pre-run checks

- [x] `start_tradingagents.ps1 -DryRun` still resolves the intended project-local
      paths.
- [x] The registry digest still matches the value recorded above.
- [x] The worktree state and configuration digest are recorded.
- [x] No prior run is active for the chosen instrument/date.
- [x] The optional-source-degraded mechanism cannot affect Instrument Identity,
      the Authoritative Market Snapshot, or another required capability.
- [x] The operator knows that a Fund with no applicable registered Fund Strategy
      Rule should complete with an Analysis Outcome. That is not an operational
      failure.

## Run matrix

### 1. Equity

- [x] Start a fresh `--no-checkpoint` run for the approved Equity.
- [x] Select only analysts permitted by its Capability Profile.
- [x] Confirm the graph signature records `debate=1|risk=1`.
- [x] Do not continue to the next run until the artifact checks below pass.

### 2. Mainland Fund

- [x] Start a fresh `--no-checkpoint` run for the approved Mainland Fund.
- [x] Confirm authoritative resolution as `instrument_kind=fund` with
      `capability_profile_id=fund.v1`.
- [x] Confirm the company-fundamentals analyst is not routed.
- [x] If no Fund Strategy Rule applies, require a completed Analysis Outcome,
      zero directional writes, and no Trading Decision.

### 3. Optional-source-degraded

- [x] Activate only the pre-approved, reversible optional-source failure
      mechanism.
- [x] Start a fresh `--no-checkpoint` Equity run.
- [x] Confirm the required Authoritative Market Snapshot remains available.
- [x] Confirm the unavailable capability is recorded as a typed Source
      Acquisition Outcome and cannot create a Source Fact.
- [x] Confirm optional loss changes availability/degradation only; it must not
      bypass a gate or enter the Validated Decision Context.
- [x] Restore the optional-source configuration immediately after the run.

## Artifact checks after every run

These checks apply to the three accepted matrix cases. The first degraded-news
attempt's pre-audit external 503 is retained separately below and is not treated
as a canonical matrix result.

- [x] `run_status.json` has a canonical run ID, immutable audit digest, terminal
      lifecycle state, null active phase, and the correct Terminal Outcome Kind.
- [x] A completed Analysis Outcome is not classified as an operational failure.
- [x] `decision-audit.json` verifies its own digest and matches the digest in
      `run_status.json`.
- [x] Audit and `runtime_metrics.json` agree on model calls, tool calls, token
      counts, acquisition attempts, retries, circuit events, and terminal route.
- [x] Unknown cost remains `available=false`, `amount_usd=null`; it is never
      rewritten as zero.
- [x] Every Trading Decision contains complete registered Decision Assertions
      over canonical Source Facts and a valid Calculation Lineage.
- [x] Provider error text, unsupported model prose, debate transcripts, and
      Advisory Commentary are absent from the Validated Decision Context.
- [x] Advisory Commentary is labeled everywhere an operator can see it.
- [x] Blocked stages show the required zero-call and zero-write counters.
- [x] Decision Report identity, Instrument Identity, Effective Trading Date,
      Evidence Integrity Status, rule predicates, and artifact references match
      the canonical audit.
- [x] Run and published report bundles are byte-identical where both copies
      exist.
- [x] No signal, position, or Trading Memory write occurs for an Analysis
      Outcome or blocked Decision Gate.

## Stop conditions

Stop the validation and do not start another run if:

- the provider-side spending limit is absent or cannot be verified;
- the approved spend or elapsed-time envelope is exceeded;
- a run uses a different provider, model, analysis date, or graph depth;
- a required capability is affected by the optional-source failure mechanism;
- telemetry is missing or cannot reconcile with the audit;
- a gate, terminal contract, report, signal, or memory invariant fails; or
- an unexpected operational failure occurs.

Record the artifact paths and typed failure category. Do not broaden the task
into an unrelated provider/fund fix unless the failure violates this
remediation's acceptance contract.

## Acceptance

- The user explicitly approves paid/networked validation before execution.
- Each run has a canonical run ID and immutable audit digest.
- No provider error text, unsupported prose, or non-rule-backed premise influences direction.
- Blocked stages show the expected zero-call/zero-write counters.
- Findings are appended to this issue and the implementation handoff.

## Comments

Checklist prepared on 2026-07-23. No live model or provider call was made while
preparing it.

### Controlled live execution on 2026-07-23/24

Execution used repository HEAD
`2cef611df3bd3094d78de431dddf5173bd413e73` in the existing broadly dirty
worktree. `git status --short` contained 71 paths; no unrelated path was staged,
committed, reset, or discarded. The launcher dry run still resolved `logs`,
`data/cache`, and `data/memory/trading_memory.md`. The production identity
registry digest remained
`a40567476d13fea6aecda9fbbf23d39ad39631fed87918a26cae9db251ff4572`.

All cases ran sequentially with checkpointing disabled, `debate=1|risk=1`,
DeepSeek SDK retries set to zero, and the approved provider, models, date, and
English output.

#### Equity: accepted

- Artifact run:
  `logs/600895.SS/2026-07-23/runs/20260723_225705`
- Canonical run ID:
  `run:68647d7667910d7c6ce16b5795d14bbaf51df9798a921f4a5fd461458cbf981f`
- Audit digest:
  `25b48c9071a2b1fc49162d9f90f63e546c65813688fdc2eae75be610a8fdcd10`
- Configuration digest:
  `config:7555836fa599ce1107304414f1dbe98191c91e07e501221f4ac36f658b6bbb07`
- Lifecycle/outcome/integrity:
  `completed` / `trading_decision` / `degraded`
- Deterministic decision: `Sell`, supported by
  `market.return_20d.sell@1.0` with comparator `less_than_or_equal`,
  threshold `-0.05`, and observed canonical 20-day return `-0.14395335`.
- Decision Assertion Coverage was 100% and the Validated Decision Context
  contained only the canonical fact and registered assertion.
- Optional news loss was recorded as typed `provider_error`, `no_data`, and
  later `circuit_open` acquisition outcomes. It degraded availability without
  blocking the rule-backed decision.
- Runtime telemetry: 20 model calls, 129,533 input tokens, 42,917 output
  tokens, 23 tool calls, 6 acquisition attempts, zero retry events, 2 circuit
  events, and zero dropped/coalesced events.
- The audit verified its own digest and reconciled with status and runtime
  telemetry. Both sharded `.utf8.gz` evidence artifacts decompressed as valid
  UTF-8 and matched their plaintext SHA-256 filenames. The deterministic report
  rendered the exact registered predicate and exposed no model-authored
  Advisory Commentary.

The outer unattended harness originally exited at the post-terminal
`Save report?` prompt. Canonical status had already finalized successfully with
no operational error. The harness was then changed to answer `N` to both
post-run prompts; this did not alter the completed canonical run.

#### Mainland Fund: accepted

- Artifact run:
  `logs/510500.SS/2026-07-23/runs/20260723_231632`
- Canonical run ID:
  `run:70a3d60f1d51507d13f017ed6a6473377a3afb435184a35dcf5d9a22f94396d4`
- Audit digest:
  `863d26d8857e5ddf136680f5d013eed1a4aadd25a57b2d05c4e14b84340de92b`
- Configuration digest:
  `config:7555836fa599ce1107304414f1dbe98191c91e07e501221f4ac36f658b6bbb07`
- Lifecycle/outcome/integrity:
  `completed` / `analysis_outcome` / `insufficient`
- Authoritative identity resolved to `510500.SS`, XSHG, `fund`, CNY. The
  registered capability mapping resolved to `fund.v1`; only market, social,
  and news analysts were selected, with no fundamentals analyst.
- The production Strategy Rule registry contains no Fund rule. Preflight
  therefore completed non-directionally with typed
  `decision_configuration_invalid`, no Validated Decision Context, no direction
  selection, no Trading Decision, and no operational error.
- Runtime telemetry: zero model calls, zero tokens, zero tool calls, 2
  successful deterministic acquisitions, zero retries/circuit events, empty
  stage activity, and zero dropped/coalesced events.
- All 24 focused reconciliation checks passed, including audit/status identity,
  telemetry, cost-null semantics, reports, and both evidence-artifact digests.

#### Optional-source-degraded Equity: stopped on operational failure

- Artifact run:
  `logs/600895.SS/2026-07-23/runs/20260723_231955`
- Configuration digest:
  `config:d415da76f421b8c2feb1c73db8f979e16e644e3a9c309dd7e0810e2789180eae`
  (the intended process-local `get_news` override is the configuration
  difference).
- Lifecycle/outcome:
  `failed` / `operational_failure`
- Typed failure: `operational_error_category=graph_execution`,
  `failed_phase=graph_stream`.
- DeepSeek returned HTTP 503 `service_unavailable_error` during the Bull
  Researcher call. SDK retries remained zero, so no retry amplification
  occurred.
- The required Instrument Identity and Authoritative Market Snapshot were
  available. Optional news acquisition recorded one typed `not_configured`
  outcome followed by one per-run `circuit_open` outcome.
- Runtime telemetry before failure: 13 model calls, 74,004 input tokens, 32,429
  output tokens, 22 tool calls, 4 acquisition attempts, zero retry events, one
  circuit event, and zero dropped/coalesced events.
- The run failed before canonical audit/report finalization, so it has no
  canonical run ID or audit digest. It produced no Validated Decision Context,
  Trading Decision, signal, position, or Trading Memory write.
- The process-local override ended with process exit. The 503 satisfies the
  ticket's unexpected-operational-failure stop condition, so the run was not
  retried and no provider/model substitution was attempted.

#### Spend reconciliation and acceptance

- DeepSeek balance before Equity: CNY 42.88.
- Immediate balance after Equity: CNY 42.79; it settled to CNY 42.69 before the
  Fund run.
- Balance after Fund: CNY 42.69.
- Immediate balance after the stopped degraded run: CNY 42.66; it settled to
  CNY 42.57 at final reconciliation.
- Total observed balance delta: CNY 0.31. Approved envelope remaining:
  CNY 32.69.

This first attempt's live acceptance was **not complete**. Equity and Fund
passed, but the degraded case could not satisfy the canonical artifact checks
after the external 503. The user subsequently authorized the single retry
recorded below.

### Authorized degraded-news retry on 2026-07-24

The user authorized exactly one additional degraded-news attempt at
2026-07-24T04:12:53-07:00. No model-based health probe was sent. Local preflight
confirmed the same repository HEAD and registry digest, no active run for the
approved symbol/date, and the provider balance endpoint reported CNY 42.57.

#### Optional-source-degraded Equity retry: accepted

- Artifact run:
  `logs/600895.SS/2026-07-23/runs/20260724_041408`
- Canonical run ID:
  `run:69006036eed70b1fba42fc4b00f79e7a2eae37c7682bb2bda4c522f4d73056e8`
- Audit digest:
  `e333cac29b677b850e5407fae4efad89b87b82c508b8e9c978afdb69d852bee4`
- Configuration digest:
  `config:d415da76f421b8c2feb1c73db8f979e16e644e3a9c309dd7e0810e2789180eae`
- Lifecycle/outcome/integrity:
  `completed` / `trading_decision` / `degraded`
- Runtime: 520 seconds, within the 20-minute envelope.
- Runtime telemetry: 21 model calls, 155,425 input tokens, 43,731 output
  tokens, 24 tool calls, 5 acquisition attempts, zero retry events, one circuit
  event, and zero dropped/coalesced events.
- Instrument Identity remained available as `600895.SS`, XSHG, `equity`, CNY,
  with `equity.v1`. AkShare returned a typed `provider_error` for the required
  market snapshot; registered BaoStock fallback then supplied the available
  Authoritative Market Snapshot for the effective date 2026-07-23.
- The process-local optional-news mechanism recorded
  `validation_unavailable / sentiment_news / not_configured`, followed by the
  expected per-run `circuit_open`. Neither outcome created a Source Fact or
  entered the Validated Decision Context.
- The validated decision remained `Sell` under
  `market.return_20d.sell@1.0`, comparator `less_than_or_equal`, threshold
  `-0.05`, with canonical BaoStock-derived 20-day return `-0.14419077`.
  Rating, rule version, comparator, threshold, polarity, target, and horizon
  matched the accepted non-forced Equity run. The independent required-snapshot
  fallback changed the source artifact and exact return, not the decision
  semantics.
- Decision Assertion Coverage was 100%. Audit self-digest, status/audit
  identity, telemetry totals/stages, unknown-cost semantics, deterministic
  report fields, closed Validated Decision Context, and both sharded artifact
  plaintext digests passed 27 of 27 focused reconciliation checks.
- Operator-visible trader prose was labeled `Advisory Commentary`; only the
  validated contract emitted `Final decision ready: Sell`.

After canonical report and status finalization, the outer Windows console
raised `UnicodeEncodeError` while trying to render a `¥` character with GBK.
This produced a nonzero harness exit but did not alter the already completed
canonical lifecycle, audit, report, or telemetry. It is a non-blocking Windows
console follow-up, not a canonical run failure. The follow-up was completed
offline on 2026-07-24: a strict GBK stream reproduced the Rich render failure,
CLI stdout/stderr now normalize to UTF-8, and regression coverage preserves
`¥` without another provider call or any canonical-artifact change.

#### Final spend and acceptance

- Balance before the retry: CNY 42.57.
- Post-run observed balance: CNY 42.36.
- Retry observed delta: CNY 0.21.
- Total observed delta from the original CNY 42.88 balance: CNY 0.52.
- Approved CNY 33 envelope remaining at the post-run observation: CNY 32.48.

Live acceptance is **complete**. The Equity, mainland Fund, and authorized
degraded-news matrix cases each have the required terminal behavior and
reconciled canonical artifacts. No further live call is authorized or needed
for ticket 13.

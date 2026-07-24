# Evidence integrity remediation follow-up plan

Status: completed

## Shared understanding

The 2026-07-22 review established that the two latest runs completed with valid
audit digests and deterministic Trading Decisions, but the committed
implementation still has gaps at checkpoint, acquisition, Instrument Identity,
telemetry, replay, and operator-oversight boundaries.

The governing objective remains the one in `spec.md`: a Trading Decision may be
published only from Decision-Ready Evidence through registered Strategy Rules
and the three fail-closed graph gates. Optional-source loss may degrade breadth;
it may not masquerade as a required-source failure or bypass the trust path.

## Vocabulary and decisions already settled

No new glossary entry or ADR is required before implementation.

- `CONTEXT.md` already defines Instrument Identity, Capability Profile,
  Authoritative Market Snapshot, Source Acquisition Outcome, Evidence
  Admission, Validated Decision Context, Advisory Commentary, Decision Gate,
  Trading Decision, and Analysis Outcome.
- ADR-0002 requires configured provider fallback to one sufficiently validated
  Authoritative Market Snapshot.
- ADR-0007 requires checkpoint-safe, closed, versioned evidence contracts.
- ADR-0008 puts all three fail-closed gates inside the compiled graph.
- ADR-0010 and ADR-0011 require registered, instrument-kind-specific Strategy
  Rules.
- ADR-0012 makes acquisition policy, not a model, own fallback, retry, and
  circuit breakers.
- ADR-0013 requires registered history and lineage for derived facts.
- ADR-0015 and ADR-0016 separate validated decisions, Advisory Commentary,
  lifecycle, terminal outcome, and evidence integrity.

## Findings converted to tickets

| Priority | Ticket | Outcome |
| --- | --- | --- |
| P0 | 14 | Evidence Admission survives checkpoint resume and concurrent runs without process-memory trust state |
| P0 | 15 | Provider selection enforces the applicable Strategy Rule history floor |
| P1 | 16 | `510500.SS` resolves authoritatively as a Fund and no fund/index inherits the generic equity return rule |
| P1 | 17 | Runtime metrics and the canonical audit preserve every retry/circuit event and stage aggregate |
| P1 | 18 | Actual 2026-07-19 evidence is replayed through production boundaries and every acceptance row is named |
| P2 | 19 | Advisory Commentary and validated decisions are unmistakable; typed diagnostics do not contradict gate status |
| P2 | 20 | Dependency floors are justified and the Pydantic construction warning is removed |
| P2 | 22 | Repository launcher runs keep output, cache/checkpoint/PIT state, and Trading Memory under explicit project-local roots |
| P3 | 21 | Model-call/token efficiency is measured and a separate optimization decision is prepared |

Tickets 14 through 20 and ticket 22 block another controlled live-model
validation. Ticket 21 remains a non-blocking optimization decision and is not
allowed to weaken a trust boundary.

## Target architecture

```text
Instrument Identity + Decision Horizon + Strategy Registry
                         |
                         v
              Evidence requirements
                         |
                         v
Provider -> AcquisitionController -> complete per-run outcome ledger
                         |                       |
                         v                       v
          Authoritative Market Snapshot    Run Telemetry
                         |                       |
                         v                       |
Canonical Source Facts -> Evidence Preflight    |
                         |                       |
                         v                       |
Evidence Admission -> checkpointed binding      |
                         |                       |
                         v                       |
Validated Decision Context -> Decision Gate <---+
                         |
              +----------+----------+
              |                     |
       Trading Decision       Analysis Outcome
              |                     |
              +----------+----------+
                         |
             Audit + report + runtime metrics
```

`DecisionPolicyEngine` is immutable policy configuration. Per-run evidence,
admission, and telemetry state travels explicitly through graph state or a
run-scoped context and is serialized at checkpoint boundaries.

## Execution order

### Phase 1: Close the two P0 trust failures

Run tickets 14 and 15 as separate TDD changes. They may be developed in
parallel only if file ownership is kept separate; both touch graph/evidence
boundaries and must be integrated before Phase 2.

Exit criteria:

- A fresh process resumes from immediately after Evidence Admission and reaches
  the same normalized terminal contract as an uninterrupted run.
- Concurrent runs cannot clear, replace, or consume each other's admitted
  evidence.
- A one-row primary provider cannot prevent a 21-row fallback provider from
  becoming the Authoritative Market Snapshot.
- If every provider is short, the run completes with an `insufficient_history`
  Analysis Outcome and no directional writes.

### Phase 2: Complete production routing and observability

Run tickets 16 and 17. The Instrument Identity work builds on the history
requirement plumbing from ticket 15. Telemetry can begin independently but must
be integrated with the same terminal state used by CLI and programmatic runs.

Exit criteria:

- The digest-pinned production registry resolves `510500.SS` as a Fund.
- Funds do not run company-fundamentals analysis and cannot use the generic
  equity return rule.
- Index support has an explicit Capability Profile or fails closed before rule
  selection.
- Every acquisition attempt, retry, fallback, and circuit rejection is retained
  in one run-level ledger.
- `runtime_metrics.json` and the canonical audit agree on overlapping totals.

### Phase 3: Prove the specification and repair oversight output

Run tickets 18 and 19 after the typed state and telemetry contracts stabilize.

Exit criteria:

- The persisted 2026-07-19 evidence fixtures pass through the same adapters,
  gates, direction selector, terminal contract, audit, and reporter used by
  production callers.
- Every acceptance-matrix row in `spec.md` maps to a named deterministic test.
- A trader's Advisory Commentary may oppose the validated rating without being
  labeled final or entering the Validated Decision Context.
- An admitted gate cannot report `deterministic_gate_rejected`.
- A valid BaoStock fallback cannot produce `snapshot_unavailable`.

### Phase 4: Stabilize dependencies and operator storage; measure cost

Run ticket 20 after the correctness suite is stable. Ticket 22 can proceed
independently because it owns launcher/configuration behavior rather than graph
trust boundaries. Triage ticket 21 using the corrected telemetry; do not
combine model-topology changes with P0/P1 fixes.

Exit criteria:

- Each dependency floor has a documented, tested reason or is reverted in a
  separate change.
- Direct model construction and JSON round trips emit no Pydantic warning.
- Repository development launches place the complete run bundle under
  `<project>/logs`, reusable cache/checkpoint/PIT state under
  `<project>/data/cache`, and Trading Memory under `<project>/data/memory`, while
  explicit overrides and installed-use defaults retain their documented
  precedence.
- Model-call and token baselines exist by stage and research depth.
- Any proposed call reduction has explicit operator-value and trust-boundary
  acceptance criteria.

## Verification strategy

Every ready-for-agent ticket follows the same loop:

1. Add the smallest failing regression at the production interface.
2. Record the expected red result.
3. Implement the smallest complete correction.
4. Run the focused tests named in the ticket.
5. Run Ruff on touched files and `git diff --check`.
6. Perform Stage 1 specification-compliance review.
7. Perform Stage 2 code-quality review.

Before ticket 13 controlled live validation:

```powershell
.\.venv\Scripts\python.exe -m pytest -q
.\.venv\Scripts\python.exe -m ruff check .
git diff --check
```

Additional release evidence:

- Recorded replay is offline and deterministic.
- Checkpoint resume and concurrency tests pass repeatedly.
- The acceptance matrix has no unmapped row.
- Audit and runtime telemetry reconcile.
- `510500.SS` resolves as `fund.v1` and cannot inherit an equity rule.
- Launcher smoke coverage proves logs, reports, status, metrics, audits,
  runtime artifacts, cache/checkpoint/PIT state, and Trading Memory use their
  intended project-local roots.
- Both review stages have no unresolved Critical or Important finding.

## Project-local runtime state

On 2026-07-22, the existing user-home runtime history was moved into the
project `logs` directory: 1,997 files totaling 32,664,990 bytes. Relative paths,
lengths, and SHA-256 digests matched after migration, and the former source was
left empty. Three historical `run_status.json` files still report `running`;
they were preserved unchanged because path migration must not rewrite audit
history. Ticket 22 makes future repository-launcher runs use the same
project-local roots.

The existing reusable cache was also moved into `<project>/data/cache`: 185
files totaling 5,482,820 bytes, again with matching relative paths, lengths, and
SHA-256 digests. The configured Trading Memory directory was empty, so there
was no `trading_memory.md` to migrate. Future authorized memory writes use
`<project>/data/memory/trading_memory.md`.

## AkShare operational baseline

On 2026-07-22, the current environment produced:

- Eastmoney history HTTP 200, JSON `rc=0`, 16 rows, no `Retry-After`.
- AkShare 1.18.73 `stock_zh_a_hist` returned 16 rows for `600895` and
  `601658`.
- The production adapter returned 16 rows for `600895.SS` and `510500.SS`.

This proves the current IP is not blocked for those history endpoints. It does
not prove that news, sentiment, or fundamentals endpoints are available. Ticket
17 must preserve typed status/error metadata so a future transient failure can
be distinguished from HTTP 403/429, proxy/DNS failure, an upstream outage, or an
AkShare parsing change.

## NOT in scope

- Predictive profitability, backtesting, portfolio optimization, execution
  simulation, or position sizing.
- A broad fund/index Strategy Rule catalog. Missing rules cause an Analysis
  Outcome.
- Provider voting or blending snapshots.
- Bypassing retry limits, circuit breakers, rate limits, or IP restrictions.
- Live-model prose equality as a test oracle.
- PIT ingestion or runtime-artifact garbage-collection redesign.
- Automatic migration or reinterpretation of historical runtime artifacts.
- Paid live validation before the release gates pass and the user approves a
  bounded budget.

## Completion definition

- Tickets 14 and 15 close P0.
- Tickets 16 through 18 close P1.
- Tickets 19, 20, and 22 close P2.
- Ticket 21 has a triage decision based on corrected telemetry.
- Ticket 13 is unblocked only after the deterministic suite and both review
  stages are clean.

## Completion record

Completed on 2026-07-23.

- Tickets 14 through 22 are completed. Ticket 21 retains the current model
  topology after measuring the corrected telemetry baseline.
- The full deterministic suite passed: 1,392 tests and 71 subtests passed, with
  two expected environment/live-test skips.
- Ruff and `git diff --check` passed.
- Specification-compliance and code-quality review found no unresolved
  Critical or Important finding.
- Ticket 13 completed under the explicit bounded authorization recorded in that
  ticket. All three controlled live-validation cases reconciled successfully.
- The post-terminal Windows GBK `UnicodeEncodeError` was reproduced offline with
  a strict GBK stream and resolved by normalizing CLI stdout/stderr to UTF-8.
  Regression coverage verifies Rich report output preserves `¥` without
  changing canonical artifacts or making another provider call.

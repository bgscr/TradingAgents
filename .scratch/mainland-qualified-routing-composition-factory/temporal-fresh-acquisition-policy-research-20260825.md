# Temporal fresh-acquisition policy research — 2026-08-25

## Scope and conclusion

This note resolves only the fresh-acquisition temporal contradiction exposed by
Ticket 07. It does not authorize live work, alter provider order, enable
`qualified_v1`, or reinterpret any historical run.

**Recommendation:** adopt a forward-only, two-instant temporal transaction
policy. Preserve the pre-acquisition clock sample, but name and use it as
`eligibility_started_at`. After all prerequisite provider proofs have been
acquired, sample one `eligibility_evaluated_at`, build the terminal calendar
binding at that instant, and emit the context-level decision once. Require

```text
eligibility_started_at
    <= observed_at
    <= retrieved_at
    <= eligibility_evaluated_at
```

for fresh evidence, with the existing `observed_at <= retrieved_at` rule still
applying to retained evidence. For `current_analysis_only`, derive
`latest_completed_session` at the terminal `eligibility_evaluated_at`; if the
transaction crosses the next open session's close, the old requested session
fails closed. Strict PIT keeps its independent cutoff/exact-revision predicate.

This is the narrowest policy that makes the promises simultaneously true
without falsifying timestamps, treating retrieval time as applicability proof,
or accepting evidence that is temporally later than the terminal evaluation.

## Deterministic contradiction

The conflict is mechanical:

1. The normative v1 term says `eligibility_evaluated_at` is injected before
   first metadata acquisition
   ([spec.md:801](spec.md#L801)). The fresh-run sequence repeats that the host
   injects it before retained-state evaluation or provider work
   ([spec.md:1024](spec.md#L1024), [spec.md:1029](spec.md#L1029)-[1034](spec.md#L1034)).
2. Production adapters truthfully timestamp an artifact only after the provider
   call returns: Tushare calls the SDK at
   [tushare_statements.py:258](../../tradingagents/dataflows/tushare_statements.py#L258)-[267](../../tradingagents/dataflows/tushare_statements.py#L267),
   then samples `self._now()` and stores it as both observation and retrieval at
   [tushare_statements.py:280](../../tradingagents/dataflows/tushare_statements.py#L280)-[287](../../tradingagents/dataflows/tushare_statements.py#L287).
   AKShare-Sina has the same ordering
   ([akshare_sina_statements.py:376](../../tradingagents/dataflows/akshare_sina_statements.py#L376)-[405](../../tradingagents/dataflows/akshare_sina_statements.py#L405));
   BaoStock samples after its request at
   [baostock_capabilities.py:1183](../../tradingagents/dataflows/baostock_capabilities.py#L1183)-[1195](../../tradingagents/dataflows/baostock_capabilities.py#L1195).
3. The run owner implements the promised ordering exactly: it samples the clock
   at [qualified_routing_eligibility_lifecycle.py:607](../../tradingagents/qualified_routing_eligibility_lifecycle.py#L607),
   builds the calendar binding at
   [qualified_routing_eligibility_lifecycle.py:614](../../tradingagents/qualified_routing_eligibility_lifecycle.py#L614)-[618](../../tradingagents/qualified_routing_eligibility_lifecycle.py#L618),
   and only later invokes metadata acquisition at
   [qualified_routing_eligibility_lifecycle.py:663](../../tradingagents/qualified_routing_eligibility_lifecycle.py#L663)-[669](../../tradingagents/qualified_routing_eligibility_lifecycle.py#L669).
4. The v1 predicate nevertheless requires
   `retrieved_at <= eligibility_evaluated_at`
   ([spec.md:859](spec.md#L859)-[866](spec.md#L866)); the implementation rejects
   the inevitable later timestamp at
   [financial_temporal_eligibility.py:672](../../tradingagents/dataflows/financial_temporal_eligibility.py#L672)-[687](../../tradingagents/dataflows/financial_temporal_eligibility.py#L687).

A tight public-seam repro therefore needs no provider or model I/O: give
`FinancialEligibilityAcquisitionContextOwner.acquire_context()` a fixed clock
`t0`, a fake provider whose returned proof has truthful
`observed_at == retrieved_at == t0 + 1 second`, and the pinned fake calendar.
The current code necessarily returns `temporal_eligibility_rejected` with
`retrieval_after_eligibility_evaluation`, leaving the context unbound; the
qualified shadow surface consequently reports `shadow_dispatch_failed`. The
existing highest-seam fresh test misses this case because it fixes the run clock
at `2026-08-11T00:59Z`
([test_financial_eligibility_metadata_acquisition.py:1675](../../tests/test_financial_eligibility_metadata_acquisition.py#L1675)-[1697](../../tests/test_financial_eligibility_metadata_acquisition.py#L1697))
but gives both fresh artifacts the earlier timestamp
`2026-08-10T16:48:07Z`
([test_financial_eligibility_metadata_acquisition.py:1803](../../tests/test_financial_eligibility_metadata_acquisition.py#L1803)-[1808](../../tests/test_financial_eligibility_metadata_acquisition.py#L1808),
[test_financial_eligibility_metadata_acquisition.py:1836](../../tests/test_financial_eligibility_metadata_acquisition.py#L1836)-[1843](../../tests/test_financial_eligibility_metadata_acquisition.py#L1843)).

The retained run evidence confirms the latest live attempt is immutable failure
evidence, not a policy oracle: run `20260825_083711` is recorded failed at
[run_status.json:20](C:/Users/56922/.tradingagents/logs/601328.SS/2026-08-25/runs/20260825_083711/run_status.json#L20)-[31](C:/Users/56922/.tradingagents/logs/601328.SS/2026-08-25/runs/20260825_083711/run_status.json#L31),
while its exact calendar preflight independently passed at
[run_status.json:34](C:/Users/56922/.tradingagents/logs/601328.SS/2026-08-25/runs/20260825_083711/run_status.json#L34)-[44](C:/Users/56922/.tradingagents/logs/601328.SS/2026-08-25/runs/20260825_083711/run_status.json#L44).
Its later DeepSeek `RemoteProtocolError` exhaustion is a separate retained
transport failure
([runtime_metrics.json:250](C:/Users/56922/.tradingagents/logs/601328.SS/2026-08-25/runs/20260825_083711/runtime_metrics.json#L250)-[280](C:/Users/56922/.tradingagents/logs/601328.SS/2026-08-25/runs/20260825_083711/runtime_metrics.json#L280));
it neither resolves nor weakens the deterministic temporal contradiction.

## Governing promises

The repository gives four decisive constraints:

- Timestamps and evidence classes remain distinct. Retrospective data may
  support a current decision, but it does not prove historical availability
  ([CONTEXT.md:39](../../CONTEXT.md#L39)-[40](../../CONTEXT.md#L40)); ADR-0026
  requires effective date, retrieval time, first observation, and revision
  identity to remain distinct
  ([0026-distinguish-backfill-from-observed-pit-history.md:7](../../docs/adr/0026-distinguish-backfill-from-observed-pit-history.md#L7)-[11](../../docs/adr/0026-distinguish-backfill-from-observed-pit-history.md#L11)).
- `observed_at` and `retrieved_at` are immutable and never rewritten
  ([spec.md:799](spec.md#L799)-[800](spec.md#L800)); provider/request/retrieval
  fields cannot manufacture effective or publication proof
  ([spec.md:807](spec.md#L807)-[821](spec.md#L821)).
- Current analysis is governed by the pinned calendar's completed-session
  frontier, not local midnight, next-session open, or a grace duration
  ([spec.md:868](spec.md#L868)-[882](spec.md#L882)). Strict PIT is independently
  governed by first publication and direct or exact-revision availability no
  later than its cutoff ([spec.md:884](spec.md#L884)-[898](spec.md#L898)).
- Evolution is forward-only. Historical plans, contexts, decisions, checkpoints,
  artifacts, and failed runs remain under their recorded contracts and are not
  re-evaluated or rewritten
  ([02G-temporal-eligibility-contracts-evaluator.md:111](issues/02G-temporal-eligibility-contracts-evaluator.md#L111)-[120](issues/02G-temporal-eligibility-contracts-evaluator.md#L120));
  exact replay performs no clock, evaluator, calendar, provider, or model work
  ([06B-deterministic-temporal-acceptance.md:31](issues/06B-deterministic-temporal-acceptance.md#L31)-[38](issues/06B-deterministic-temporal-acceptance.md#L38)).

Ticket 07 additionally fixes authority: qualified output remains
`shadow_non_authoritative`, legacy remains authoritative, and `qualified_v1`
remains disabled
([07-real-two-symbol-shadow-rollout-gate.md:83](issues/07-real-two-symbol-shadow-rollout-gate.md#L83)-[94](issues/07-real-two-symbol-shadow-rollout-gate.md#L94),
[07-real-two-symbol-shadow-rollout-gate.md:148](issues/07-real-two-symbol-shadow-rollout-gate.md#L148)-[157](issues/07-real-two-symbol-shadow-rollout-gate.md#L157)).

## Policy options and consequences

| Option | Consequence | Decision |
| --- | --- | --- |
| Rewrite or clamp fresh `retrieved_at` to the pre-acquisition instant | Makes the predicate pass by falsifying provenance; contradicts immutable truthful timestamps and ADR-0026. | Reject. |
| Replace the rule with retrieval-local-date ordering or a grace window | Reintroduces the exact authority the temporal amendment retired and behaves incorrectly across weekends, holidays, and session-close boundaries. | Reject. |
| Drop `retrieved_at <= eligibility_evaluated_at`, or exempt all fresh evidence without a terminal bound | Fixes ordinary latency but permits a terminal decision whose evidence is later than its stated evaluation instant; a session-close crossing is not detected. | Reject. |
| Set evaluation to `max(retrieved_at)` | Lets evidence timestamps manufacture the evaluation/calendar authority and does not prove when the transaction actually finalized. | Reject. |
| Evaluate each artifact at its own post-call instant | Avoids the direct inequality but allows company and listing subdecisions to straddle different frontiers and abandons the one terminal context decision. | Reject. |
| Two-instant, single-flight transaction with one terminal evaluation | Preserves truthful times, detects clock/session-frontier crossings, keeps one terminal decision for all followers, and is exactly replayable from persisted inputs. | **Adopt.** |

## Recommended forward policy amendment

Freeze all v1 readers and identities. Add a v2 policy with these normative
rules:

1. `eligibility_started_at` is sampled once by the single-flight leader before
   retained-state inspection or provider work. It pins the exact calendar
   revision and transaction identity; it is not an evidence-applicability field
   and is not the terminal eligibility decision time.
2. Providers preserve their actual `observed_at` and `retrieved_at`. Fresh proof
   must bind the active run/transaction, artifact/revision/row, provider
   subrequest key, and physical attempt identity. A missing or mismatched fresh
   transaction binding fails closed.
3. After all required company-type and BaoStock lifecycle proofs are present,
   but before context construction/binding or dispatcher exposure, the leader
   samples one timezone-aware `eligibility_evaluated_at`. No follower samples a
   clock. The terminal evaluator consumes all selected proofs once under that
   instant.
4. Fresh proof requires
   `eligibility_started_at <= observed_at <= retrieved_at <= eligibility_evaluated_at`.
   Retained proof continues to require `observed_at <= retrieved_at <=
   eligibility_evaluated_at`. Any clock inversion or post-evaluation evidence
   is a typed rejection.
5. `current_analysis_only` additionally keeps the open-session and inclusive
   requested-session-close predicates, and requires
   `latest_completed_session(pinned_revision, eligibility_evaluated_at) ==
   requested_session`. Thus a transaction that crosses the next open session's
   close rejects rather than silently extending the old frontier.
6. `strict_historical_pit` keeps the existing `f_ann_date` and
   direct-pre-cutoff/exact-revision predicates unchanged. The new transaction
   interval never substitutes for strict historical availability proof.
7. Provider-derived `evidence_effective_as_of`, `ann_date`, and applicable
   `f_ann_date` rules remain unchanged. Neither start/end time nor retrieval
   time supplies those fields.
8. Persist both transaction instants, the pinned calendar revision, terminal
   binding/frontier, selected proof identities, typed result/reasons, and the
   context-level decision identity. Replay validates those persisted values
   with zero current clock/calendar/evaluator/provider/model I/O.
9. If acquisition, proof projection, terminal evaluation, context binding, or
   interruption fails, expose no partial callable runtime and preserve original
   failure precedence. No later retry resumes or repairs the failed namespace.

The forward identity cascade should be explicit rather than silently changing a
v1 promise: `provider-neutral-temporal-eligibility-v2`,
`market-session-eligibility-binding-v2`, a v2 metadata temporal decision, a v2
transaction binding, and bumped containing request/context/checkpoint/ledger/
manifest projections. Because the routing plan pins these material contracts,
use a new plan version/signature (conservatively plan `1.4`) and a new context
version (conservatively context v3). Preserve raw provider artifact/candidate
identities and every v1/plan-1.3/context-v2 reader unchanged.

## Required deterministic proof before requalification

The minimum red/green matrix should include:

- Public fresh seam with zero latency and with one second of company and
  lifecycle latency: both bind one terminal context; actual timestamps remain
  unchanged.
- Retrieval after the terminal clock, clock regression, missing transaction
  binding, and mismatched artifact/attempt identity: typed fail-closed results,
  no context or dispatcher.
- Acquisition starting before and finishing at/after the next session close:
  old requested session rejects at the exact close boundary.
- Retained evidence before the terminal instant, retained future-dated
  evidence, next-day/weekend/holiday cases, and the original
  `2026-08-11 00:48:07 Asia/Shanghai` control.
- Strict direct-pre-cutoff and exact-revision controls unchanged; post-cutoff
  fresh retrieval does not become strict-PIT eligible merely by belonging to
  the transaction.
- Thirty-two followers: one start clock, one end clock, one calendar revision,
  one acquisition leader, exact physical attempts, one terminal context result.
- Exact v1 replay and new v2 replay with sentinels proving zero clock, calendar,
  evaluator, provider, coordinator, attempt, or model work.
- Version/identity mutation tests, historical-byte inventory, legacy authority,
  `shadow_non_authoritative`, and disabled `qualified_v1` regressions.

No live provider or model I/O was used in this research.

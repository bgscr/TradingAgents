# 02F — Truthful provider indicator normalization and compatibility evaluation

**Parent specification:** [Mainland qualified routing production composition factory](../spec.md)

**What to build:** Make every qualified financial-indicator provider project
only metadata it actually proves, then evaluate all retained candidates through
the provider-neutral contracts from Ticket 02E. Complete-but-incompatible
candidates remain visible with their completeness and typed metadata rejection;
only unresolved families continue sequentially through the existing immutable
Capability Routing Plan, where each lower provider is judged solely from its own
truthful artifact-bound metadata.

**Blocked by:** 02E — Financial-indicator semantic metadata contracts.

**Status:** completed

## Observable behavior

- Tushare keeps its stored v1 normalized indicator artifacts and existing raw
  candidate identities. Where an assessment-bearing result projection is
  required, it writes `tushare-financial-indicator-result-v2` and binds
  `financial-indicator-metadata-compatibility-v1` without relabeling v1 data.
- AKShare writes `akshare-financial-indicators-v2`: absent provider value
  currency remains raw `XXX`, absent filing scope remains raw `UNKNOWN`, and
  neither request metadata nor Instrument Identity is represented as provider
  proof.
- BaoStock writes `baostock-ratio-family-normalizer-v2` under the same truthful
  no-backfill rule while retaining BaoStock artifact evidence. Its transport/
  result envelope remains `baostock-ratio-family-adapter-v1`.
- Tushare, AKShare, BaoStock, and Yahoo candidates enter one provider-neutral
  compatibility evaluation. No provider identity changes assessment rules.
- A family that passes the unchanged 100% critical and at-least-90%
  declared-core completeness gates but has a material `UNPROVEN` or mismatched
  metadata dimension is retained and rejected for selection. Its artifact,
  candidate, completeness assessment, compatibility assessment, raw marker, and
  typed reason remain observable.
- `NOT_APPLICABLE` is ignored only when every retained selected field's pinned
  field-semantics declaration makes that dimension irrelevant. Raw `UNKNOWN` is
  never treated as `NOT_APPLICABLE` or compatible proof.
- After each provider evaluation, only unresolved families continue through the
  plan-owned sequence. Resolved families stop; lower providers receive no work
  for them. There is no fan-out, hidden retry, or provider-specific router path.

## Acceptance criteria

- [ ] All normalizers consume the immutable Ticket 02E registry and emit enough
  truthful provider-origin unit/basis, raw currency/scope markers, publication/
  observation lineage, and artifact bindings for the shared evaluator; they do
  not infer policy from populated fields.
- [ ] Requested `CNY` and `CONSOLIDATED`, Financial Eligibility Acquisition
  Context intent, Instrument Identity currency, provider order, or installed
  provider identity is never copied into provider artifacts/candidates as value
  currency or filing-scope evidence.
- [ ] For Tushare `fina_indicator`, complete dimensionless ratio,
  percentage/rate, and turnover values can reach 100% completeness while value
  currency assesses `NOT_APPLICABLE`, raw currency remains retained, filing
  scope assesses material `UNPROVEN`, and every affected family remains
  unselected.
- [ ] Missing Tushare `f_ann_date` independently limits the artifact to
  current-only at most and prevents strict no-lookahead, strict historical
  Source Fact, and strict replay eligibility.
- [ ] AKShare and BaoStock normalizers preserve their own truthful raw markers
  and evidence. Yahoo is evaluated under its existing degraded/current-only
  evidence contract. A weakness in one provider neither qualifies nor
  disqualifies another.
- [ ] Complete-but-incompatible and partial candidates remain retained in the
  manifest input with separate completeness and metadata outcomes; a material
  `UNPROVEN`/mismatch rejects selection without deleting the candidate.
- [ ] Compatibility is applied before selection at the existing trust boundary.
  Only selected candidates can project Source Facts or Decision-Ready evidence;
  Evidence Admission itself is not broadly rewritten or versioned.
- [ ] Only unresolved families continue sequentially through the immutable
  Capability Routing Plan. Existing provider routes, stop rules, ADR-0031
  coordination, cooldown/retry budgets, single-flight, cache reuse, and physical-
  attempt accounting remain authoritative.
- [ ] BaoStock remains the status/lifecycle/strict-history authority; daily
  mainland routing remains AKShare → BaoStock → Yahoo; qualified results remain
  `shadow_non_authoritative`.

## Exact contract and version requirements

- Consume `financial-indicator-field-semantics-v1` and
  `financial-indicator-metadata-compatibility-v1` with identity namespace
  `financial-indicator-metadata-compatibility:v1`.
- Add `tushare-financial-indicator-result-v2` only where the result/assessment
  projection must carry compatibility; keep
  `tushare-financial-indicator-adapter-v1` artifacts and raw candidate identities
  unchanged.
- Add `akshare-financial-indicators-v2`.
- Add `baostock-ratio-family-normalizer-v2`; keep
  `baostock-ratio-family-adapter-v1` transport/result envelope unchanged.
- Produce compatibility bindings required by `financial-provider-candidate-v2`
  and `financial-period-selection-v3`; persistence/projection publication belongs
  to Ticket 05B.
- Consume Capability Routing Plan payload 1.2 without changing routes, policy
  version, financial-period eligibility v2, completeness thresholds, or the
  `mainland-routing-plan:v1:<sha256>` identity namespace.

## Deterministic regressions

- For every provider, cover complete compatible, complete incompatible,
  incomplete, raw `UNKNOWN`/`XXX`, mismatched, `NOT_APPLICABLE`, missing
  publication lineage, and typed terminal/retryable outcomes.
- Assert byte-for-byte that requested CNY/scope and Instrument Identity currency
  never appear in provider-origin metadata fields when the provider omitted
  them.
- Prove Tushare complete statements can stop statement fallback independently
  of complete-but-scope-unproven indicator families, which remain retained and
  continue to lower indicator providers only.
- Exercise mixed resolved/unresolved families and prove exact plan-order calls,
  stop behavior, retained candidates, typed reasons, cache/bulk reuse, and one
  attempt event per actual transport call with zero selection-only attempts.
- Run identical compatibility fixtures against Tushare, AKShare, BaoStock, and
  Yahoo to prove provider-neutral assessment and absence of provider-specific
  router exceptions.
- Regress the unchanged 100% critical / at-least-90% declared-core gates,
  financial-period eligibility v2, current-only/strict-PIT boundaries, Evidence
  Admission, BaoStock authority, daily routing, and shadow disposition.

## Historical compatibility

- Existing Tushare v1 and BaoStock adapter-v1 artifacts/results, raw candidate
  identities, historical AKShare output, manifests, selections, reports,
  checkpoints, audits, and IDs remain readable and are never relabeled or
  rewritten.
- Historical replay uses the compatibility/semantics contract pinned in the
  recorded plan. New policy is never retroactively applied to historical
  candidates.
- New plan-1.2 work writes only the forward normalization/result contracts named
  above. Ticket 05B owns durable forward projections and zero-I/O restoration.

## Explicit non-goals

- Route/order/fallback changes, a second router, provider-specific exceptions,
  provider fan-out, speculative calls, hidden retries, or provider-probing.
- Request currency/scope backfill, Instrument Identity as value evidence,
  companion-filing inference, completeness changes, financial-period
  eligibility changes, or relaxed current-only/strict-PIT rules.
- Broad Evidence Admission rewrite, canonical Evidence/Source Fact/Decision
  version change, checkpoint/report persistence work owned by Ticket 05B, or
  historical migration/reinterpretation.
- Live providers, paid models, real credentials, integration tests, copying
  either prototype implementation, changing BaoStock/daily-route authority, or
  enabling authoritative `qualified_v1`.

## Verification commands

Run deterministic provider normalization, selection, and routing tests only:

```powershell
pytest -q tests/test_tushare_indicator_adapter.py tests/test_akshare_indicator_normalization.py tests/test_baostock_ratio_adapters.py
pytest -q tests/test_financial_indicator_metadata_compatibility.py tests/test_financial_capability_routing_selection.py tests/test_financial_period_eligibility.py
pytest -q tests/test_provider_subrequest_cache.py tests/test_provider_request_coordinator.py tests/test_mainland_physical_attempt_coordination.py
pytest -q tests/test_admitted_evidence_binding.py tests/test_recorded_evidence_replay.py tests/test_current_tradeability.py tests/test_market_snapshot.py
ruff check .
git diff --check
git status --short
```

No command may select integration tests or contact a live provider or model.

## Definition of done

- [ ] Tushare result v2 where required, AKShare normalizer v2, and BaoStock
  normalizer v2 truthfully preserve provider metadata without request backfill.
- [ ] One provider-neutral evaluator retains and rejects complete incompatible
  candidates and advances only their unresolved families through the plan.
- [ ] Deterministic tests prove material `UNPROVEN` rejects, pinned
  `NOT_APPLICABLE` handling, exact attempt cardinality, and unchanged routing,
  completeness, PIT, authority, and Evidence Admission boundaries.
- [ ] Historical readers pass without relabeling, migration, or semantic
  reinterpretation.
- [ ] Spec Compliance and Code Quality review find no unresolved Critical or
  Important issue; no live traffic occurred, no prototype code was copied, and
  authoritative `qualified_v1` remains disabled.

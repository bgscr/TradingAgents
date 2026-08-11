# 02G — Add temporal eligibility contracts and evaluator

**Parent specification:** [Mainland qualified routing production composition factory](../spec.md)

**What to build:** Add the closed provider-neutral temporal contracts and one
deterministic evaluator that decide whether company-type and listing/lifecycle
metadata is eligible for the requested mainland session. Current analysis uses
the pinned Market Session Calendar's session-bounded completed-session frontier;
strict historical PIT uses its independent exact-cutoff predicate. The evaluator
returns one immutable accepted/rejected decision with exact typed reasons and
introduces no provider implementation or I/O.

**Blocked by:** 02A — Add provider-neutral eligibility acquisition context
contracts; ADR-0036 — Drive mainland freshness from a persisted session
calendar.

**Status:** ready-for-agent

## Observable behavior

- A caller supplies immutable provider proof, the temporal mode, a pinned
  `market-session-eligibility-binding-v1`, and one fixed
  `eligibility_evaluated_at`; the evaluator returns exactly `accepted` or
  `rejected` as a `financial-metadata-temporal-eligibility-decision-v1` with a
  stable content identity.
- New writes use the terms `requested_session`, `evidence_effective_as_of`,
  `ann_date`, `f_ann_date`, `observed_at`, `retrieved_at`,
  `eligibility_evaluated_at`, `strict_cutoff`,
  `exact_revision_authoritative_available_at`, and
  `latest_completed_session` with exactly the meanings in the specification.
- For `current_analysis_only`, evidence for `requested_session` remains eligible
  only while that session equals the pinned calendar's
  `latest_completed_session` at the fixed evaluation instant. The frontier
  advances at the next open session's close, never at local midnight or session
  open.
- For `strict_historical_pit`, eligible `f_ann_date` and exact availability by
  `strict_cutoff` are required independently of the current-analysis frontier.
  Qualified exact-revision proof can satisfy availability only when it binds
  the exact immutable revision.
- Accepted decisions derive `eligible_session=requested_session`; rejected
  decisions have no eligible session and carry a sorted, duplicate-free tuple
  from the exact closed rejection-reason vocabulary.

## Acceptance criteria

- [ ] Common proof validation requires immutable timezone-aware timestamps with
  `observed_at <= retrieved_at`, qualified non-null
  `evidence_effective_as_of <= requested_session`, provider-origin non-null
  `ann_date <= requested_session`, and exact artifact/row/revision binding.
- [ ] `current_analysis_only` additionally requires an open requested session,
  inclusive `retrieved_at >= session_close(requested_session)`,
  `retrieved_at <= eligibility_evaluated_at`, and exact equality between
  `requested_session` and the calendar-derived `latest_completed_session`.
- [ ] The next open session's close is the exact frontier transition: before
  close the prior session can remain current; at and after close a new decision
  for the prior session rejects. Weekends and mainland holidays follow the
  pinned calendar without an elapsed grace window.
- [ ] `strict_historical_pit` requires `f_ann_date <= requested_session` and
  either both immutable observation/retrieval timestamps no later than
  `strict_cutoff`, or qualified
  `exact_revision_authoritative_available_at <= strict_cutoff` bound to the
  exact immutable revision.
- [ ] The evaluator never treats request intent, a route, factory default,
  symbol/name, model output, retrieval date, or derived eligible session as
  provider proof; missing or contradictory proof fails closed.
- [ ] No exact retrieval-local-date equality survives as authority and no such
  rule is replaced by local-date `<=`/`>=`, a grace duration, next-session open,
  or another scattered temporal predicate.
- [ ] The Market Session Calendar proves session/open/close/frontier facts only;
  provider evidence proves material effective/publication facts only. Neither
  authority substitutes for the other.

## Exact contract and version requirements

- Add `provider-neutral-temporal-eligibility-v1` with closed modes
  `current_analysis_only` and `strict_historical_pit`, exact inclusive/exclusive
  boundaries, proof rules, and immutable rejection vocabulary.
- Add `market-session-eligibility-binding-v1` and identity namespace
  `market-session-eligibility-binding:v1:`. Bind calendar revision ID,
  Reference Market/timezone, requested session/open status and close, next open
  session/close, fixed evaluation instant, and derived latest-completed session.
- Add `financial-metadata-temporal-eligibility-decision-v1` and identity
  namespace `financial-metadata-temporal-eligibility-decision:v1:`. Bind mode,
  exact artifact/revision, every temporal proof field, policy/calendar
  identities, result, eligible session, and typed reasons.
- Advance Capability Routing Plan payload to `1.3`, pinning the temporal policy,
  calendar binding, decision projection, and all downstream temporal versions
  while preserving every plan-1.2 semantic-compatibility binding. Keep the
  `mainland-routing-plan:v1:<sha256>` namespace; material bindings change its
  digest.
- Define Financial Eligibility Acquisition Context v2 forward bindings for the
  requested session/mode, fixed evaluation instant, calendar binding,
  latest-completed-session decision, both prerequisite temporal decisions, and
  strict proof where applicable. Integration and persistence belong to 02H and
  05C.
- The exact closed v1 rejection reasons are:
  `requested_session_not_open_in_pinned_calendar`,
  `calendar_eligibility_binding_mismatch`, `observation_after_retrieval`,
  `evidence_effective_as_of_unproven`,
  `evidence_effective_after_requested_session`, `ann_date_missing`,
  `ann_date_after_requested_session`,
  `retrieval_before_requested_session_completed`,
  `retrieval_after_eligibility_evaluation`,
  `requested_session_not_latest_completed_frontier`,
  `f_ann_date_missing_for_strict_pit`,
  `f_ann_date_after_requested_session`,
  `exact_revision_not_available_by_strict_cutoff`,
  `temporal_evidence_artifact_binding_mismatch`, and
  `unknown_temporal_eligibility_mode`.

## Migration and compatibility behavior

- The change is forward-only. Historical plans, contexts, artifacts,
  checkpoints, decisions, and failed runs remain readable under their recorded
  temporal contracts and are never relabeled, rewritten, or re-evaluated.
- Plan 1.3/context v2 rejects old, absent, unknown, or mismatched temporal
  bindings with a typed compatibility outcome or the existing explicit
  `START_NEW` path. No contract here invents proof or upgrades state in place.
- Plan 1.2/context v1 behavior remains historical evidence only; the new
  evaluator does not reinterpret the failed Ticket 07 run.

## Explicit non-goals

- Provider adapters, proof projection, acquisition integration, checkpoint
  persistence, live providers/models, integration tests, credentials, or
  copying/importing the temporal prototype implementation.
- A local-date equality/ordering authority, next-day or elapsed grace window,
  next-session-open cutoff, simple `<=`/`>=` substitution, or duplicated
  evaluator logic.
- Route/order/fallback, completeness, financial-period eligibility, Evidence
  Admission, Decision Gate, BaoStock authority, daily market routing, or
  `shadow_non_authoritative` changes.
- Enabling, recommending, or exercising authoritative `qualified_v1` or running
  Ticket 07.

## Deterministic regressions

- Exhaustively test every closed rejection reason, reason ordering and
  deduplication, identity stability, and identity change for each material
  input.
- Use a pinned fake Market Session Calendar and fixed clock to test session-close
  inclusivity, the next-close frontier transition, weekends, holidays, invalid
  calendar bindings, and unknown modes.
- Test strict direct pre-cutoff acceptance, qualified exact-revision acceptance,
  and fail-closed missing/later `f_ann_date`, post-cutoff, generic-publication,
  and artifact-mismatch controls.
- Prove all exact temporal terms remain distinct and immutable and that request,
  retrieval-date, local-date, route, symbol/name, factory, and model backfill is
  rejected.
- Regress plan 1.2 readers, plan 1.3 deterministic signatures, Financial Period
  eligibility v2, indicator semantic compatibility, ADR-0031 coordination, and
  ADR-0036 calendar authority.

## Verification commands

Run deterministic contract and calendar tests only:

```powershell
pytest -q tests/test_financial_metadata_temporal_eligibility.py tests/test_market_session_eligibility_binding.py
pytest -q tests/test_financial_eligibility_acquisition_context.py tests/test_financial_capability_routing_plan.py
pytest -q tests/test_market_session_calendar.py tests/test_financial_period_eligibility.py
pytest -q tests/test_financial_indicator_field_semantics.py tests/test_financial_indicator_metadata_compatibility.py
ruff check .
git diff --check
git status --short
```

No command may select integration tests or contact a provider or model.

## Definition of done

- [ ] All three temporal contracts and identities emit the exact specified
  versions, terminology, predicates, and closed reason taxonomy.
- [ ] Current-analysis and strict-PIT boundary matrices pass without local-date,
  grace-window, or proof-authority substitution.
- [ ] Plan 1.3/context-v2 forward bindings are exact and historical readers
  remain unchanged with no migration or reinterpretation.
- [ ] Spec Compliance then Code Quality review finds no unresolved Critical or
  Important issue; no provider/model I/O occurred, no prototype code was copied,
  and authoritative `qualified_v1` remains disabled.

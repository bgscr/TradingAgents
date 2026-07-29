# `/to-spec` amendment: admit qualified Tushare 2,000-point capabilities

## Amendment scope

Amend the proposed mainland capability-qualified acquisition specification with
the live Tushare requalification performed on 2026-07-29. This amendment does
not change the Current Analysis Provider Chain globally, does not change the
initial Strict History Provider, and does not authorize production code or
routing changes by itself.

## Replace requirement 7

Replace:

> Keep Tushare financial/status/factor/name capabilities disabled for the
> current token. Requalification after an entitlement change is required before
> enabling any individual capability.

With:

> Admit Tushare only through independently gated capability routes. The
> `income`, `balancesheet`, and `cashflow` endpoints are eligible for the
> full-statement route after `comp_type`-specific normalization, period-level
> completeness, `f_ann_date` admission, duplicate/revision preservation, and
> typed-fallback tests pass. `fina_indicator` is eligible for current indicator
> acquisition but is not strict point-in-time evidence without a validated
> first-publication binding. `adj_factor` is eligible as a native factor source
> but does not by itself qualify a complete Provider History Bundle.
> `suspend_d` is supplemental event evidence only and cannot satisfy complete
> per-session suspension/status history. `namechange` is eligible for dated name
> history only; ST routing remains disabled until a positive ST-transition
> fixture passes twice. No result in this qualification changes global provider
> priority or BaoStock's initial Strict History Provider status.

## Add requirements

11. Normalize Tushare statements by `comp_type` before completeness scoring.
    Preserve the returned union schema in the provider artifact, but never score
    bank payloads against industrial-company-only fields or infer the production
    core from whichever fields happen to be populated in one response.
12. Key statement candidates by symbol, `end_date`, `report_type`, `comp_type`,
    `f_ann_date`, and preserved provider revision. Retain `ann_date` and
    `update_flag`; do not treat the binary `update_flag` as a globally unique
    restatement identifier.
13. A Tushare statement row cannot enter the Validated Decision Context before
    `f_ann_date`. A `fina_indicator` row, which lacks `f_ann_date`, must bind to
    an admitted filing revision or remain current-only/degraded.
14. Keep raw statement payloads immutable and assign a content digest. A value
    change under the same provider metadata publishes a new local revision and
    cannot overwrite an earlier Observed Point-in-Time revision.
15. Configure Tushare under one Upstream Service Identity in the Provider
    Request Coordinator. The provider-published 2,000+ tier ceiling is 200
    calls/minute and 100,000 calls/day per API; the Operator Safety Ceiling must
    be lower, centrally shared, and independently configurable. Qualification
    traffic must not discover limits by inducing throttling.
16. Do not use an empty `suspend_d` frame as proof of per-session traded status.
    Authoritative status still requires a row or derived state for every open
    session under the Suspension Observation contract.
17. Split `name_history` from `st_status_history`. `namechange` may satisfy the
    former after effective-date validation. The latter requires a cohort fixture
    containing a real ST or `*ST` transition plus comparison to an authoritative
    dated status source.
18. Tushare factor admission is capability-local. Strict reconstruction still
    falls back by a complete single-provider Provider History Bundle; it must
    never combine Tushare factors with another upstream's raw observations or
    status merely because the dates align.

## Add acceptance criteria

- Replay the four-symbol, seven-endpoint cohort twice with 56 physical calls,
  no retries, no credential leakage, and no production-state writes.
- Require 2/2 granted statement calls for all four symbols; five latest annual
  and eight latest reporting periods must pass the declared company-type core,
  except that the recent-listing policy accepts every eligible period since the
  authoritative listing date.
- Require 100% `ann_date`, `f_ann_date`, `report_type`, `comp_type`, and
  `update_flag` coverage on every accepted statement period.
- Include `comp_type=1` and `comp_type=2` fixtures. A bank must pass a declared
  bank core rather than a dynamically pruned provider union.
- Require two-run equality of period sets, schemas, filing metadata, and
  canonical payload digests. Any later value change must create explicit
  provider revision lineage.
- Permit `fina_indicator` in current analysis only after five annual and eight
  reporting periods pass its declared core. Bar strict point-in-time use until
  first-publication binding is implemented.
- Require a native dated factor payload twice for every cohort symbol, while
  retaining BaoStock first in strict bundle routing.
- Keep authoritative suspension/status qualification failed until a positive
  suspension fixture and complete per-session status semantics pass twice.
- Keep ST-history qualification failed until a positive ST-transition fixture
  passes twice; ordinary name histories alone are insufficient.
- Verify typed `PERMISSION_DENIED`, `RATE_LIMITED`, empty-success, and provider
  failure outcomes without retry bursts or fallback fan-out.

## Capability-specific order after implementation acceptance

1. Full statements: Tushare -> AKShare-Sina -> Yahoo.
2. Financial indicators: Tushare -> AKShare -> missing BaoStock ratio families
   -> Yahoo profile snapshot.
3. Adjustment factors: BaoStock -> Tushare -> AKShare -> Yahoo-derived factor.
4. Suspension/status: BaoStock authoritative; Tushare supplemental event lookup
   only; AKShare/Yahoo current-only.
5. Name events: Tushare -> AKShare. Dated ST state: BaoStock; no Tushare ST
   fallback until the positive-event gate passes.

The existing global market-snapshot order remains AKShare -> BaoStock -> Yahoo.


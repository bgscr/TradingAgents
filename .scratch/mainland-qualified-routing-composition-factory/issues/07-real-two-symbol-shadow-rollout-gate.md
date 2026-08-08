# 07 — Run the authorized real two-symbol shadow rollout gate

**Parent specification:** [Mainland qualified routing production composition factory](../spec.md)

**What to build:** After Ticket 06 passes and the operator gives fresh explicit
authorization for live-provider use, run the shipped built-in production
composition through the real CLI validation seam for \`601328.SS\` and then
\`600895.SS\`, one at a time for the same latest completed mainland session.
Retain complete safe evidence showing exact/non-duplicated provider attempts,
BaoStock Current Tradeability authority, report and decision-audit publication,
\`shadow_non_authoritative\` qualified output, unchanged authoritative legacy
output, and still-disabled \`qualified_v1\`.

**Blocked by:** 06 — Run deterministic production-composition acceptance.

**Status:** blocked

**Gate state:** Blocked by Ticket 06. This remains the only live-provider gate,
and authoritative `qualified_v1` remains disabled.

## Observable behavior

- The operator first confirms Ticket 06's traceability package is complete and
  explicitly authorizes this live-provider gate under the configured Data Usage
  Mode and provider terms. Without that fresh authorization, stop without
  opening provider/model resources.
- Both runs use the shipped static built-in selection with no directly injected
  composition, no \`MODULE:CALLABLE\` operator factory, no test module, and no
  dynamic-import fixture.
- A newly allocated run/checkpoint namespace and retained content-addressed
  artifacts are used for each attempt. No historical report, checkpoint,
  attempt, artifact, audit, snapshot, pin, provider row, or ID is cleared or
  reused to manufacture a clean result.
- Each run originates and binds one immutable Financial Eligibility Acquisition
  Context without a manually seeded context, runtime, or checkpoint, and retains
  its safe version, identity, and projection before ordinary financial dispatch.
- Run \`601328.SS\` first as the bank Equity and \`600895.SS\` second as the
  non-bank Equity, sequentially, for the same latest completed mainland session.
- Each run retains its safe preflight result, Capability Routing Plan signature,
  dispatcher/cache checkpoint projection, provider physical-attempt events,
  manifest, complete report, and decision audit.
- Both runs avoid \`qualified_routing_composition_missing\`, complete through
  the registered composition, coordinate every physical call, establish
  BaoStock Current Tradeability authority, and publish qualified observations
  as \`shadow_non_authoritative\` while legacy-selected output remains
  authoritative.

## Acceptance criteria

- [ ] Ticket 06 is complete with passing QCF1–QCF18, PE1–PE15, parent AC1–AC13,
  full-suite-twice, Ruff, diff, and two-stage-review evidence before the first
  live call.
- [ ] Fresh operator authorization, Data Usage Mode, credential boundary,
  Operator Safety Ceilings, pacing, exact plan signature, and latest completed
  mainland session are recorded safely without copying credentials.
- [ ] Authoritative \`qualified_v1\` is verified disabled immediately before
  the first run.
- [ ] \`601328.SS\` completes first through the real CLI
  \`qualified_v1_shadow\` validation seam using only the shipped built-in
  composition; its report and decision audit are complete and retained.
- [ ] \`600895.SS\` starts only after the bank run terminates and completes
  through the same seam and policy inputs in its own new run/checkpoint
  namespace; its report and decision audit are complete and retained.
- [ ] Each symbol's fresh built-in run acquires company type and authoritative
  BaoStock listing provenance, binds exactly one immutable eligibility context
  before factory/dispatcher exposure, and retains its safe context identity and
  projection with no manual seed.
- [ ] For both runs, every transport call has exactly one existing-schema
  physical-attempt event; retries/fallback match configured budgets and plan
  order; cache hits, reuse, skips, selections, and derived facts add zero
  attempts; no hidden retry, duplicate bulk call, fan-out, or uncoordinated
  request appears.
- [ ] For both runs, BaoStock is the authority for Current Tradeability.
  Tushare names and supplemental sources establish no suspension, ST,
  lifecycle, or tradeability authority; strict Provider History Bundles are not
  blended.
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

- Before live work, rerun the Ticket 06 focused production-composition gate and
  the non-integration suite from the reviewed code state; any failure stops the
  rollout.
- Capture pre/post hashes or immutable references for representative historical
  material and prove the two runs do not rewrite it.
- Audit exact provider-attempt accounting against the immutable plan separately
  for each symbol, including retries, skips, cache reuse, and sequential
  fallback.
- Compare report/audit authority fields, legacy-selected outputs, BaoStock
  status provenance, and \`qualified_v1\` disabled preflight before/after.
- If a run fails, preserve it unchanged. A retry requires a new run identity and
  repeats the preflight/accounting checks.

## Compatibility and migration behavior

- Each live run is additive and uses a fresh namespace with existing schemas,
  ID constructors, coordinator, cache, artifact store, dispatcher, checkpoint,
  graph, report, and audit seams.
- Historical state remains immutable and readable. No cleanup, migration,
  relabeling, ID renumbering, checkpoint upgrade, or failed-run rewrite is
  permitted.
- Existing provider routes, completeness thresholds, financial eligibility,
  Evidence Admission, Decision Gate, BaoStock authority, daily AKShare →
  BaoStock → Yahoo routing, and legacy authority remain unchanged.
- A typed provider/configuration failure is a gate failure, not permission to
  change policy or repair historical state live.

## Explicit non-goals

- Enabling or recommending authoritative \`qualified_v1\`, changing its
  activation gate, or treating a shadow pass as an authority decision.
- Direct composition injection, explicit operator factory, test factory,
  dynamic import for the built-in, provider qualification, route experimentation,
  threshold/eligibility changes, or alternative provider order.
- Clearing historical data, reusing a failed run identity, rewriting a failed
  run, live checkpoint/artifact repair, hidden retries, provider fan-out,
  account rotation, or speculative prewarming.
- Code changes during rollout unless a reproducible runtime defect is confirmed
  against deterministic evidence. Policy/data-quality/provider-availability
  outcomes are not code defects.

## Verification commands

Before seeking live authorization:

\`\`\`powershell
pytest -q tests/test_qualified_routing_composition_contract.py tests/test_production_qualified_provider_composition.py tests/test_qualified_routing_static_registration.py tests/test_qualified_routing_managed_lifecycle.py tests/test_qualified_routing_factory_checkpoint_restore.py
pytest -q tests/test_financial_eligibility_acquisition_context.py tests/test_financial_eligibility_metadata_acquisition.py tests/test_financial_request_factory_context_binding.py
pytest -q tests/test_qualified_production_source_bindings.py tests/test_qualified_routing_eligibility_context_lifecycle.py tests/test_qualified_routing_checkpoint_context_restore.py
pytest -q tests/test_qualified_routing_rollout.py tests/test_corrected_shadow_joint_acceptance.py
pytest -q -m "not integration"
ruff check .
git diff --check
git status --short
tradingagents analyze --help
\`\`\`

After Ticket 06 passes and the operator explicitly authorizes live-provider
execution, invoke the following command once per symbol. On the first invocation
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

- [ ] The retained gate record links Ticket 06's passing evidence and the fresh
  operator authorization to two sequential, uniquely identified CLI runs.
- [ ] Both symbols have complete safe preflight, plan, cache/dispatcher,
  physical-attempt, manifest, report, and decision-audit evidence satisfying
  every acceptance criterion.
- [ ] Exact attempt accounting and BaoStock Current Tradeability authority are
  independently reconciled for each symbol with no unexplained or duplicated
  call.
- [ ] Legacy output remains authoritative, every qualified artifact is
  \`shadow_non_authoritative\`, historical material is untouched, and
  authoritative \`qualified_v1\` remains disabled after the gate.
- [ ] No code changed during rollout, unless a reproducible runtime defect was
  first confirmed and returned to the deterministic implementation/review gate;
  any such change requires Ticket 06 to pass again before retrying live.

# 01 — Add the production composition contract and immutable blueprint

**Parent specification:** [Mainland qualified routing production composition factory](../spec.md)

**What to build:** Add the shipped, non-test contract for a managed qualified
routing composition and its first deterministic phase: validate an explicitly
requested \`qualified_v1_shadow\` workload completely, then produce an immutable,
plan-bound, secret-free composition blueprint before any resource is opened.
This slice is observable through the public factory seam: a valid request returns
a stable blueprint/managed-composition identity, while every invalid request
fails through the existing typed capability-routing preflight boundary with zero
graph, model, provider, store, artifact, report, or checkpoint work.

**Blocked by:** None — can start immediately.

**Status:** ready-for-agent

## Observable behavior

- The production contract accepts the authoritative Capability Routing Plan,
  Instrument Identity/run asset configuration, financial eligibility binding,
  explicit validation intent, safe configuration, dependency availability, and
  credential availability as construction inputs.
- Before opening resources, it validates mode, plan signature, asset binding,
  enabled-capability source coverage, Data Usage Mode, runtime roots, account
  scope, pacing, Operator Safety Ceilings, dependency availability, and the
  \`TUSHARE_TOKEN\` presence required by enabled routes.
- A valid request captures immutable safe values and emits a deterministic,
  secret-free factory/blueprint identity. Later mutation of the input mapping or
  process environment cannot change routes, identities, pacing, ceilings, usage
  mode, or source coverage.
- The contract accepts only an explicit validation workload whose immutable plan
  mode is \`qualified_v1_shadow\`. It defensively rejects \`qualified_v1\`, even
  if called below normal preflight, and does not weaken the existing earlier
  authority-disabled rejection.
- Time, sleeping, run owner identity, and provider transport construction remain
  injectable boundaries for deterministic tests; production registration is
  deferred to Ticket 03.

## Acceptance criteria

- [ ] The immutable blueprint is sufficient for later run binding without
  consulting mutable input config, mutable environment, installed-provider
  inference, or a second provider-order table.
- [ ] Validation completes before any durable store, artifact directory,
  provider client/session, coordinator, cache, graph, model client, report, or
  checkpoint is created; deterministic spies prove zero construction on every
  rejected case.
- [ ] Invalid mode, absent validation intent, invalid plan or Instrument Identity
  signature, eligibility mismatch, invalid Data Usage Mode/runtime root/account
  scope/pacing/Safety Ceiling, missing dependency or credential, incomplete plan
  source coverage, and unsafe factory output all produce an existing typed,
  payload-free capability-routing preflight failure.
- [ ] The Tushare credential is read only through the existing environment
  boundary and passed later to a per-run constructor. Its value, hash, fragment,
  or credential-bearing URL is absent from config, plan, blueprint, repr,
  identity, cache keys, errors, logs, and serialized projections.
- [ ] The factory identity is deterministic for identical safe inputs, changes
  only for material safe construction inputs, and does not create or replace any
  existing run, request, sequence, attempt, subrequest, or artifact ID namespace.
- [ ] The contract exposes one owner-capable managed composition boundary for
  later run binding, interruption, and close without constructing those
  resources in this ticket.
- [ ] \`qualified_v1\` remains disabled and \`legacy\`, ordinary
  non-validation shadow, and empty/non-applicable plan cases remain
  non-constructing.

## Deterministic regressions

- Fixed valid \`qualified_v1_shadow\` inputs produce equal blueprints and safe
  factory identities across repeated processes and mapping order.
- Mutating the original config or \`TUSHARE_TOKEN\` after construction does not
  mutate the captured blueprint; secret canaries never appear in any public or
  serializable value.
- A table-driven rejection matrix covers every pre-open validation dimension and
  proves no resource-constructor spy was called.
- Direct calls with \`qualified_v1\`, legacy, missing validation intent, and
  incomplete capability coverage fail or return no composition exactly as the
  specification requires.
- Existing plan-signature, Instrument Identity, financial eligibility, and safe
  operator-factory validation tests remain unchanged.

## Compatibility and migration behavior

- This is an additive production contract. Existing injected compositions and
  explicit operator factories remain valid; their precedence is implemented in
  Ticket 03.
- No historical report, checkpoint, artifact, manifest, attempt, audit,
  provider row, snapshot, pin, or ID is read for migration, rewritten,
  relabeled, deleted, or renumbered.
- Existing schemas, ID constructors, Capability Routing Plan, Instrument
  Identity, financial eligibility binding, and typed preflight outcomes are
  reused. No router, cache, persistence authority, provider registry, or
  checkpoint sidecar is introduced.
- Legacy and daily mainland behavior remain unchanged, including AKShare →
  BaoStock → Yahoo current-market routing and BaoStock authority.

## Explicit non-goals

- Constructing provider adapters/transports, opening run resources, static host
  registration, lifecycle cleanup, or checkpoint restoration; Tickets 02–05
  own those slices.
- Enabling or recommending authoritative \`qualified_v1\`.
- Changing routes, provider order, completeness, financial eligibility,
  revision selection, Evidence Admission, Decision Gate, Strategy Rules,
  BaoStock authority, daily market routing, or \`shadow_non_authoritative\`.
- Live-provider calls, paid-model calls, provider qualification, speculative
  prewarming, dependency upgrades, or credential persistence.

## Verification commands

\`\`\`powershell
pytest -q tests/test_qualified_routing_composition_contract.py tests/test_mainland_capability_routing.py tests/test_financial_period_contracts.py
pytest -q tests/test_qualified_routing_rollout.py -k "preflight or plan or factory or secret"
ruff check .
git diff --check
git status --short
\`\`\`

All tests in this ticket must use fixed clocks, fake sleepers, constructor spies,
fake environment boundaries, and fake dependencies. They must not make network
requests or construct paid-model clients.

## Definition of done

- [ ] The public construction seam demonstrates deterministic success and typed,
  secret-free pre-open failure for the full validation matrix.
- [ ] Tests prove the blueprint is immutable, plan-bound, source-complete, and
  independent of later config/environment mutation.
- [ ] Tests prove authoritative \`qualified_v1\` and every non-validation path
  construct zero qualified resources.
- [ ] Focused tests, Ruff, whitespace, secret-canary, and scope checks pass.
- [ ] Self-review confirms the slice adds only construction contract/blueprint
  behavior and preserves every authority and compatibility invariant in the
  parent specification.


# 03 — Add static registration and caller precedence

**Parent specification:** [Mainland qualified routing production composition factory](../spec.md)

**What to build:** Make an explicitly requested \`qualified_v1_shadow\`
validation reach the shipped composition through the existing host builder from
both CLI and programmatic analysis, without test injection or a configured
factory path. Preserve the exact override order—direct injected composition,
explicit operator \`MODULE:CALLABLE\`, then statically registered built-in—and
prove all ordinary paths allocate zero qualified resources.

**Blocked by:** 01 — Add the production composition contract and immutable
blueprint.

**Status:** completed

## Observable behavior

- The existing host composition builder owns one static reference to the shipped
  built-in factory. Built-in selection uses no dynamic import, test module,
  runtime registry mutation, or configured factory reference.
- A directly injected composition is returned/used first and neither the
  explicit operator factory nor built-in factory is invoked.
- Without direct injection, a valid explicit operator \`MODULE:CALLABLE\`
  remains second. Its reference is validated exactly as today, removed from the
  safe config passed to the callable, and it wins over the built-in.
- Without either override, the built-in is selected only for explicit validation
  intent, an immutable \`qualified_v1_shadow\` plan, and at least one enabled
  qualified capability that requires a composition.
- The CLI default callback, CLI \`analyze\` command, and programmatic analysis
  entry point invoke the same host builder and apply the same plan-signature,
  safe-output, validation-intent, and failure behavior.
- A selected managed composition reaches graph construction through the existing
  graph seam; graph construction does not discover factories and performs no
  provider I/O.

## Acceptance criteria

- [ ] CLI and programmatic valid-shadow tests select the statically registered
  built-in, reach the graph with the plan-bound managed composition, and do not
  emit \`qualified_routing_composition_missing\`.
- [ ] Direct-injection tests prove both factories have zero calls; explicit
  operator-factory tests prove the built-in has zero calls and existing
  \`MODULE:CALLABLE\`, safe-config, and plan-signature validation is unchanged.
- [ ] Built-in selection requires no test factory, test-only module, configured
  factory path, dynamic import, or mutable provider registry.
- [ ] Legacy mode, ordinary non-validation shadow analysis, empty or
  non-applicable plans, and plans with no composition-requiring capabilities
  create no coordinator store, cache, artifact directory, provider client,
  session, adapter, worker, or qualified I/O.
- [ ] Attempted authoritative \`qualified_v1\` fails with the existing
  authority-disabled preflight before any built-in-factory call; defensive
  factory rejection from Ticket 01 remains intact.
- [ ] Invalid or unsafe factory output becomes the existing typed, secret-free
  capability-routing preflight failure before graph/model/provider work.
- [ ] Direct programmatic callers may still construct and inject a composition;
  this compatibility does not create a second host-selection implementation.

## Deterministic regressions

- Spy all three candidates in one table: direct injection calls neither factory;
  operator factory calls only the operator; built-in fallback calls only the
  static factory.
- Run the no-override valid-shadow scenario through both Typer entry paths and
  direct programmatic analysis; assert an identical factory identity and plan
  signature at the graph seam.
- Run legacy, non-validation shadow, empty/non-applicable plan, disabled
  capability, and \`qualified_v1\` controls with constructor/network/model spies
  fixed at zero.
- Preserve malformed/missing operator factory, safe-config redaction, factory
  exception, plan mismatch, and incomplete composition-output regressions.
- Compare pre/post-registration legacy-selected output and qualified resource
  construction counts.

## Compatibility and migration behavior

- Override precedence is an API compatibility contract: direct composition >
  explicit operator factory > built-in. No existing override is removed or
  weakened.
- Default/legacy and ordinary non-validation behavior remains zero-qualified-
  resource and zero-qualified-I/O. Existing CLI flags and programmatic injection
  remain supported.
- The registration is static construction wiring, not a provider registry,
  routing-policy registry, service locator, or mutable module-global client.
- No historical configuration, report, checkpoint, artifact, attempt, manifest,
  audit, snapshot, pin, provider row, or ID is migrated or rewritten.

## Explicit non-goals

- Changing the Capability Routing Plan, provider routes/order, completeness,
  financial eligibility, Evidence Admission, Decision Gate, BaoStock authority,
  daily routing, or shadow authority.
- Performing provider-adapter work owned by Ticket 02, lifecycle work owned by
  Ticket 04, or checkpoint restoration owned by Ticket 05.
- Enabling \`qualified_v1\`, auto-running shadow validation for ordinary
  analysis, adding dynamic discovery for the built-in, or requiring a test
  factory.
- Live providers, paid models, qualification runs, prewarming, hidden retries,
  or historical-state changes.

## Verification commands

\`\`\`powershell
pytest -q tests/test_qualified_routing_static_registration.py
pytest -q tests/test_qualified_routing_rollout.py tests/test_cli_config_precedence.py tests/test_cli_run_status.py
pytest -q tests/test_mainland_capability_routing.py tests/test_financial_graph_dispatch.py
ruff check .
git diff --check
git status --short
\`\`\`

Use fake factories, fake compositions, fake graphs, and constructor spies only.
The tests must not construct real provider or model clients.

## Definition of done

- [ ] QCF1–QCF3 registration/precedence behavior is demonstrated at CLI and
  programmatic seams with named deterministic tests.
- [ ] Built-in selection needs no configured factory and all legacy/ordinary
  controls prove zero qualified allocation and I/O.
- [ ] Existing direct and operator override behavior passes unchanged.
- [ ] Focused tests, Ruff, whitespace, and scope checks pass; authoritative
  \`qualified_v1\` remains disabled.

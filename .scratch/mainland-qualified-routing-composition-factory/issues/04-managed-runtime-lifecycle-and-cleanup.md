# 04 — Implement managed runtime lifecycle and cleanup

**Parent specification:** [Mainland qualified routing production composition factory](../spec.md)

**What to build:** Give each production composition one managed run lifetime
covering construction, graph initialization/execution, checkpoint/report
publication, interruption, and normal completion. The composition must be the
single owner of its coordinator authority/store/peers, Provider Subrequest
Cache, immutable artifact store, provider clients/transports/adapters, stop
signal, leases, and worker activity, and must deterministically release every
owned resource on success or any \`BaseException\`.

**Blocked by:** 01 — Add the production composition contract and immutable
blueprint.

**Status:** completed

## Observable behavior

- After canonical run identity is known, one composition binds exactly one
  runtime. Rebinding the same run returns the same runtime; binding another run
  requires another composition execution scope.
- A construction-local ownership stack closes resources in reverse order if any
  acquisition step fails; no partially constructed runtime or managed
  composition escapes.
- BaoStock login/session creation is lazy and serialized within the run. A run
  that never needs BaoStock never logs in. A used or failed session logs out on
  terminal request failure, interruption, partial construction failure, or
  close.
- The analysis owner closes the composition after checkpoint/report completion
  and before returning. Graph initialization, provider dispatch, graph execution,
  checkpoint publication, \`KeyboardInterrupt\`, \`SystemExit\`, and any other
  \`BaseException\` first interrupt as needed and then close.
- Interruption sets a run-owned stop signal before cleanup. New adapter/cache
  work is rejected; pacing/wait loops observe the stop; in-flight non-terminal
  state is abandoned through existing typed, secret-free outcomes rather than
  published as available.
- \`interrupt\` and \`close\` are individually and jointly idempotent. Cleanup
  exceptions are retained for diagnostics but never replace the original
  construction or execution failure.

## Acceptance criteria

- [ ] One explicit owner accounts for every coordinator connection/authority
  store, cache, artifact store, provider client/session/transport, adapter,
  lease, stop signal, and factory-owned worker; ownership is not split across
  CLI, graph, adapter modules, or process globals.
- [ ] Failure injection after every construction step proves reverse-order
  cleanup, no returned partial object, no leaked file/DB/session/worker, and the
  original \`BaseException\` remains primary even when one or more cleanup
  operations fail.
- [ ] Failure before and after graph compilation, during dispatch/execution, and
  during checkpoint/report publication invokes lifecycle cleanup exactly once.
  \`KeyboardInterrupt\` and \`SystemExit\` are covered explicitly.
- [ ] Repeated and concurrent \`interrupt\`/\`close\` calls are harmless,
  deterministic, and do not double-logout, double-close, publish contradictory
  outcomes, or mask prior failures.
- [ ] New work after interruption/close fails with an existing typed,
  secret-free interrupted/abandoned outcome; stopped in-flight work is never
  cached/checkpointed as available.
- [ ] Normal close releases the same owned resources without manufacturing a
  failure outcome and joins all factory-owned worker activity.
- [ ] Parallel run fixtures prove clients, caches, stop signals, sessions, and
  state are run-owned with no mutable process-global provider state.

## Deterministic regressions

- Parameterize construction failure at each resource boundary and assert exact
  reverse close order plus original-error precedence under cleanup failures.
- Exercise unused, successful, terminal-failure, interrupt-during-login, and
  repeated-close BaoStock paths; assert lazy single login and exact single
  logout where applicable.
- Inject \`Exception\`, \`KeyboardInterrupt\`, and \`SystemExit\` at graph
  initialization, dispatch, graph execution, checkpoint publication, and report
  completion.
- Race interrupt against cache acquisition and coordinator pacing; assert stop
  observation, no new physical call, no available cache publication, released
  leases, closed peers/store, and joined workers.
- Run two compositions concurrently with distinct fake transports and assert no
  session/cache/client/stop-state leakage.

## Compatibility and migration behavior

- Lifecycle capability extends the existing managed composition/analysis owner;
  it does not move routing or persistence authority or change adapter outcomes.
- Existing Provider Request Coordinator, Provider Subrequest Cache, immutable
  artifact store, checkpoint safe boundary, and graph seams remain the resource
  implementations.
- Legacy and ordinary non-validation paths construct no managed qualified
  runtime and therefore acquire/close no qualified resources.
- Existing terminal outcomes, historical checkpoints/artifacts/attempts, and
  normal report behavior remain unchanged; no cleanup path rewrites prior state.

## Explicit non-goals

- Process-global provider clients, global SDK mutation, daemon resources outside
  run ownership, or a second coordinator/cache/artifact owner.
- Changing provider routing, retries, pacing policy, Safety Ceilings,
  completeness, eligibility, BaoStock authority, Evidence Admission, Decision
  Gate, daily routing, or shadow authority.
- Treating \`KeyboardInterrupt\` or \`SystemExit\` as ordinary success,
  swallowing the original failure, or serializing interrupted in-flight results
  as available.
- Live providers, paid models, provider qualification, background prewarming, or
  historical-state repair.

## Verification commands

\`\`\`powershell
pytest -q tests/test_qualified_routing_managed_lifecycle.py
pytest -q tests/test_provider_subrequest_cache.py tests/test_provider_request_coordinator.py
pytest -q tests/test_qualified_routing_rollout.py tests/test_cli_run_status.py tests/test_checkpoint_resume.py
ruff check .
git diff --check
git status --short
\`\`\`

All resources must be deterministic fakes or disposable existing stores. Tests
must not open live provider sessions or instantiate paid-model clients.

## Definition of done

- [ ] QCF14's full construction, execution, interruption, and cleanup matrix
  passes, including \`BaseException\`, idempotency, lazy BaoStock, and original
  failure precedence.
- [ ] Every runtime resource has one documented owner and deterministic terminal
  behavior; no process-global mutable provider state exists.
- [ ] Leak/race tests prove no dangling session, connection, lease, store, or
  worker after success or failure.
- [ ] Focused tests, Ruff, whitespace, secret-safety, and scope checks pass.

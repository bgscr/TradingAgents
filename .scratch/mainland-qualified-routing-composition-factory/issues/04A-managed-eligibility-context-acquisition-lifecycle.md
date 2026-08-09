# 04A — Integrate managed eligibility-context acquisition

**Parent specification:** [Mainland qualified routing production composition factory](../spec.md)

**What to build:** Integrate fresh eligibility-context acquisition with the
existing managed composition lifecycle. The graph/run host owns one immutable,
single-assignment Financial Eligibility Acquisition Context slot; concurrent
first financial calls join one acquisition single-flight; and the host binds the
complete context before it exposes ordinary financial factories or a callable
shadow dispatcher. Any partial acquisition or bind failure exposes no callable
runtime and preserves the original failure while managed cleanup completes.

**Blocked by:** 02A — Add provider-neutral eligibility acquisition context
contracts; 02B — Acquire fresh company type and listing provenance; 02C —
Integrate pure financial request factories; 02D — Add executable production-
source wiring; 04 — Implement managed runtime lifecycle and cleanup.

**Status:** completed

## Observable behavior

- Each qualified graph/run host owns exactly one context slot for one canonical
  run; neither the managed composition, request factories, adapters, CLI, nor a
  process-global object owns mutable eligibility state.
- The first current qualified financial call starts one provider-neutral
  context-acquisition single-flight. Concurrent callers join the leader and
  receive the same complete value or the same typed terminal failure.
- A successful context is bound atomically before statement/indicator factories
  or the financial shadow dispatcher can be obtained or called. Identical
  rebind returns the same value; a non-identical second bind fails typed.
- Blueprint creation and managed-runtime assembly remain zero-I/O. Context
  acquisition begins only when the current qualified financial request needs it
  and uses resources already owned by the managed composition.
- On acquisition, validation, binding, graph initialization, dispatch,
  execution, or checkpoint-publication failure—including `KeyboardInterrupt`
  and `SystemExit`—the context slot remains unbound unless the complete value was
  atomically assigned, new work stops, and owned resources are interrupted and
  closed in reverse order.
- Cleanup diagnostics never replace the original acquisition/execution failure;
  interrupt and close remain idempotent, and no partial factory, dispatcher,
  router handle, context, or callable runtime escapes.

## Acceptance criteria

- [ ] The graph/run host—not the composition or a global—owns one immutable
  single-assignment context slot bound to the canonical run identity.
- [ ] Concurrent first financial calls execute exactly one compatible context
  acquisition/resolution/listing sequence and publish exactly one atomic bind.
- [ ] Ordinary financial factories and the dispatcher are inaccessible and
  uncallable before successful context binding; successful exposure uses the
  pure Ticket 02C factories and the same plan-bound router/runtime.
- [ ] Identical rebind is idempotent, while cross-run/asset/plan/eligibility/
  context rebind fails typed without changing the original slot value.
- [ ] Every partial-acquisition and lifecycle failure path exposes no callable
  runtime, preserves the original failure as primary, signals interruption,
  and releases or terminally abandons all owned resources exactly once.
- [ ] Legacy, ordinary non-validation shadow, empty/non-applicable plan, and
  authoritative `qualified_v1` controls allocate no context slot work,
  qualified provider resources, or I/O.

## Deterministic regressions

- Use barriers and deterministic fake clocks/sleepers/transports to race many
  identical first calls and prove one acquisition, one resolver call, one
  listing acquisition/reuse decision, one bind, and identical follower results.
- Race identical and non-identical binds and prove deterministic single-
  assignment semantics with no partial mutation.
- Inject failure/interruption before and after each acquisition, validation,
  bind, factory exposure, dispatcher exposure, graph initialization/execution,
  and checkpoint-publication boundary.
- Verify reverse-order cleanup, stop-signal observation, exact close counts,
  original-failure precedence, cleanup-error retention, and harmless repeated
  interrupt/close.
- Assert legacy/disabled controls and blueprint/runtime construction perform
  zero context/provider/model/store/artifact/report/checkpoint work.

## Compatibility and migration behavior

- Lifecycle integration extends the existing managed composition and graph/run
  host seams; it creates no new process-global owner, runtime registry, router,
  cache, checkpoint sidecar, or resource lifecycle.
- Existing direct composition injection and explicit operator factory
  precedence remain unchanged. Legacy and non-validation paths retain zero-
  qualified-work behavior.
- Historical checkpoints, reports, artifacts, attempts, IDs, provider rows, and
  failed runs are never rewritten or repaired by lifecycle cleanup.

## Explicit non-goals

- Provider/source implementation, route/provider-order changes, request-
  contract design, new classification/listing policy, or checkpoint context
  persistence/restoration.
- Process-global context, mutable/mergeable/refreshable context, partial bind,
  callable-before-bind escape hatch, second router, hidden retry, provider fan-
  out, or speculative prewarming.
- Live providers, real credentials, paid models, integration tests, or the real
  two-symbol rollout gate.
- Copying/importing fresh-run prototype implementation or tests.
- Enabling or recommending authoritative `qualified_v1`.

## Verification commands

Run deterministic lifecycle, acquisition, graph, and dispatcher suites only:

```powershell
pytest -q tests/test_qualified_routing_eligibility_context_lifecycle.py tests/test_qualified_routing_managed_lifecycle.py
pytest -q tests/test_financial_eligibility_metadata_acquisition.py tests/test_financial_request_factory_context_binding.py
pytest -q tests/test_financial_graph_dispatch.py tests/test_financial_tool_dispatcher.py tests/test_qualified_routing_static_registration.py
ruff check .
git diff --check
git status --short
```

No verification command may select integration tests or contact a provider or
model.

## Definition of done

- [ ] Concurrency evidence proves one run-owned slot, one first-use single-
  flight, one atomic bind, idempotent identical rebind, and typed non-identical
  rebind failure.
- [ ] Every partial/failure/interruption boundary proves no callable runtime
  escaped, cleanup is exact/idempotent, and the original failure remains
  primary.
- [ ] Spec Compliance review confirms ownership, exposure ordering, lifecycle,
  authority, compatibility, and all non-goals.
- [ ] Code Quality review confirms race safety, error durability, deterministic
  cleanup, maintainability, and no Critical or Important findings.
- [ ] No live traffic occurred, no prototype code was copied, and authoritative
  `qualified_v1` remains disabled.

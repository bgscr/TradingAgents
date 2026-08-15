# ISO-01A — Executable Ticket 06 Sandbox acceptance mode

**Parent specification:** [Mainland qualified routing production composition factory](../spec.md)

**What to build:** Extend the existing qualified Windows Sandbox runner with
one closed, executable `ticket06_acceptance` mode beside
`boundary_qualification`. The new mode must run the complete Ticket 06
acceptance ledger inside one guest, export only bounded identity-bound evidence,
and leave the trusted host responsible for QCF17 clause B and the final QCF18
decision.

**Blocked by:** ISO-01 — Versioned Windows Sandbox runner/config and exact
disposable workspace; ISO-02 — Hash-locked offline runtime/dependency bundle
and manifest; and the current ISO-03 qualification findings recorded below.
Those findings are historical input evidence, not a dependency on the future
replacement ISO-03 run.

**Blocks:** Replacement/requalification ISO-03 on the materially changed runner
identities; the new additive post-qualification baseline freeze; and ISO-04.

**Status:** ready-for-agent

## Confirmed blocker evidence — 2026-08-14

The attempted ISO-04 result is `qualification_invalidated`. The qualified
ISO-03 runner cannot execute Ticket 06 acceptance because:

- the existing guest bootstrap accepts only `boundary_qualification`;
- the bootstrap invokes only the boundary-qualification orchestrator;
- the boundary qualification contract requires `ticket06_executed=False`;
- the sealed runner manifest contains only boundary-qualification files;
- `ticket06_acceptance` exists only as a reserved enum value; and
- there is no executable Ticket 06 acceptance runner, deterministic command
  ledger, or bounded acceptance-export contract.

No Sandbox was launched, no Ticket 06 command ran, and no protected host state
changed. This finding does not replace, relabel, or invalidate the historical
ISO-03 evidence or any frozen baseline; it only establishes that those
qualified identities are insufficient for ISO-04.

## Observable behavior

- Extend the existing Windows Sandbox runner architecture only. Do not create
  a second launcher, containment boundary, runner architecture, or acceptance
  path.
- The closed execution-mode dispatch accepts exactly
  `boundary_qualification` and `ticket06_acceptance`; the existing boundary
  qualification behavior remains executable and unchanged, and any unknown
  mode fails closed before project execution.
- The existing guest bootstrap dispatches by the validated execution mode and
  invokes one guest-side Ticket 06 acceptance orchestrator for
  `ticket06_acceptance`.
- The acceptance orchestrator executes sequentially, in the same guest:
  persistence-isolation suites; CAL-01 through CAL-04; 06B; QCF1–QCF18;
  PE1–PE15; AC1–AC13; live-test isolation; `pytest -q -m "not integration"`;
  two separate `pytest -q` full-suite passes; Ruff over `tradingagents`, `cli`,
  and `tests`; `git diff --check`; and every other executable Ticket 06 check
  currently required.
- Mandatory failure stops subsequent execution at the first point required by
  the contract. Bounded summaries for commands already attempted are retained;
  commands not attempted remain explicitly distinguishable.
- The completion marker is written exactly once and last, only after the
  bounded result has been finalized.

## Closed deterministic command ledger

Every command entry binds all of the following in canonical order:

- a stable command ID;
- an argv vector, never a shell command string assembled from untrusted data;
- a closed working-directory role;
- an environment-policy identity;
- an expected result category;
- a fixed ordering position;
- a bounded timeout policy; and
- required or optional status.

The ledger has one deterministic identity. Identical material produces the
same identity; any material argv, policy, category, ordering, timeout, or
required-status change produces a different identity. Execution follows only
the sealed ledger order and does not accept guest/user-provided command
interpolation.

## Bounded acceptance result contract

For `execution_mode=ticket06_acceptance`, the machine-readable bounded export
must bind:

- reviewed source commit/tree/archive and reconstructed tree identity;
- runtime bundle identity;
- runner, bootstrap, Sandbox config, protected-root, mapping, control, and
  environment-policy identities;
- the frozen baseline identity supplied as an input;
- the command-ledger identity;
- QCF1–QCF18 results, with QCF17 limited to guest clause A;
- PE1–PE15 and AC1–AC13 results;
- persistence-isolation, CAL-01 through CAL-04, 06B, and live-isolation
  results;
- non-integration, full-suite A, and full-suite B summaries;
- Ruff and diff/whitespace results;
- every executable review/check summary required by Ticket 06; and
- bounded no-network, no-live, no-provider, and no-model evidence.

The export rejects missing, duplicate, unknown, incomplete, contradictory, or
out-of-order criteria and undeclared/unbounded output. Command summaries are
bounded and contain no credentials, raw provider/model material, runtime
databases, host-state copies, or unsafe logs.

The guest may report only QCF17 clause A. It must not decide QCF17 host clause B
or claim final QCF17 PASS. It must not claim final QCF18 PASS without the later
host authority. ISO-04 remains the final authority after the trusted HOST AFTER
inventory and comparison.

## Containment and environment policy

- Preserve the qualified Windows Sandbox mapping policy: networking disabled;
  protected host roots unmapped; source, runtime, runner, and control inputs
  read-only; exactly one fresh writable bounded result mapping; guest-local
  writable source/runtime/state; no credentials; no host venv; and no writable
  host checkout.
- Keep every repository-defined `RUN_LIVE_*` value at `0` and
  `PYTHON_DOTENV_DISABLED=1` before project import, collection, command
  execution, and export.
- Use only sandbox-local runtime, temp, cache, data, history, checkpoint,
  report, log, payload, pytest-temp, and Python-cache state.
- Never invoke network, package-index, provider, calendar-refresh, live-test,
  or model work. No paid model or live authorization is available to the guest.

## Sealed runner and identity transition

- Extend the sealed runner manifest to include every acceptance orchestrator,
  command-ledger, support, and export-validation file. Missing, extra,
  reordered, or identity-mismatched runner material fails closed.
- The new bootstrap dispatch, orchestrator, ledger, result contract, and sealed
  manifest materially change the qualified runner identity.
- After ISO-01A PASS, rebuild/reseal source and runtime inputs as needed, then
  rerun actual ISO-03 Windows Sandbox qualification on all new identities.
- The previous ISO-03 qualification remains historical only. Do not overwrite,
  delete, reinterpret, or reuse it as qualification for the new identities.
- Only replacement ISO-03 PASS permits one new additive post-qualification
  baseline freeze. Only that new baseline permits ISO-04 to be retried.

## Deterministic regressions

- Prove `boundary_qualification` still works through the existing path,
  `ticket06_acceptance` is now executable, and unknown modes fail closed.
- Prove command-ledger identity stability, material-change sensitivity, fixed
  ordering, and correct mandatory-failure stopping behavior.
- Prove the bounded export rejects missing required criteria and preserves
  bounded summaries for attempted and not-attempted commands.
- Prove the guest cannot mark QCF17 final PASS and cannot mark QCF18 final PASS
  without host authority.
- Prove no network/provider/model/live operation is available, every
  `RUN_LIVE_*` remains `0`, and dotenv loading remains disabled.
- Prove the completion marker is last.
- Prove every required acceptance runner, ledger, support, and export-validation
  file is present in the sealed runner manifest and affects its identity.

## Explicit non-goals

- Running actual ISO-04 acceptance, any Ticket 06 acceptance command, Ticket 07,
  a provider, a paid model, or a live/integration opt-in while implementing this
  ticket.
- Launching Windows Sandbox as part of the ticket-set update or treating a
  deterministic simulated regression as replacement ISO-03 qualification.
- Freezing, replacing, deleting, or modifying any baseline or historical
  ISO-03/ISO-04 evidence.
- Enabling or recommending `qualified_v1`.
- Creating a parallel runner or weakening the qualified containment policy.

## Definition of done

- [ ] The existing runner has a closed executable `ticket06_acceptance` mode
  beside the preserved `boundary_qualification` mode, with unknown modes
  rejected before execution.
- [ ] One sealed deterministic ledger and guest orchestrator cover every
  executable Ticket 06 command in fixed sequential order with bounded timeout,
  failure, and summary behavior.
- [ ] The mode-specific bounded export binds every required identity/result,
  keeps QCF17 clause B and final QCF18 under host authority, and writes its
  completion marker last.
- [ ] Containment, guest-local state, no-network/no-live/no-provider/no-model,
  credential absence, and environment-policy regressions pass.
- [ ] All required files are sealed and identity-bound; the material runner
  identity change is recorded without altering historical evidence or a
  baseline.
- [ ] No actual ISO-04/Ticket 06/Ticket 07 work, Sandbox qualification, provider
  or model work, baseline freeze, historical repair, or `qualified_v1`
  activation occurred.


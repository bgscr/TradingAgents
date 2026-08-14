# ISO-01 — Versioned Windows Sandbox runner/config and exact disposable workspace

**Parent specification:** [Mainland qualified routing production composition factory](../spec.md)

**What to build:** Deliver a versioned host launcher, protected-root manifest,
Windows Sandbox configuration, sealed reviewed-source input, disposable
guest-local workspace, and bounded result-export contract that together make
the intended QCF17 OS boundary inspectable before any Ticket 06 command is
allowed to run.

**Blocked by:** Production bootstrap exact-ID readiness after CAL-01 → CAL-02 →
CAL-03 → CAL-04. This prerequisite is complete and persisted, so ISO-01 is the
current frontier.

**Status:** ready-for-agent

## Observable behavior

- The host launcher accepts only a clean reviewed `HEAD`/tree, records the
  commit and tree identities, inventories the reviewed tracked content, and
  creates a sealed source archive/input without executing project code or Git
  hooks.
- The launcher rejects dirty tracked state, staged/tree mismatch, untracked
  credential material, unresolved protected roots, unexpected reparse points,
  non-empty result folders, and any mapping outside the closed mapping contract.
- Source, runner, path-redacted control inventory, and runtime-bundle input when
  applicable are mapped read-only. Exactly one fresh, empty, bounded
  results-export folder is writable on the host.
- The sealed source is copied to one sandbox-local writable source
  copy/worktree. Its reconstructed tree identity must equal the reviewed tree
  before any executable acceptance work can begin.
- The guest receives writable local roots for `USERPROFILE`/`HOME`, `TEMP`/`TMP`,
  the source copy, Python environment/caches, pytest temp, payloads, and every
  TradingAgents runtime/data/cache/history/checkpoint/report/log root.
- The bounded export schema identifies the reviewed source, runner/config,
  protected manifest, mapping inventory, validation outcomes, and completion
  state without trusting a guest verdict as host authority.
- ISO-01 performs contract/static validation only. It does not run Ticket 06,
  Ticket 07, provider/model work, or Windows Sandbox boundary qualification.

## Exact OS containment requirements

- Generate a versioned Microsoft-supported `.wsb` configuration with
  `<Networking>Disable</Networking>` and `ProtectedClient` enabled where the
  installed Windows Sandbox version supports it.
- Disable clipboard redirection and minimize vGPU, audio/video input, printer,
  and other unnecessary host integrations. Any retained integration must be
  reviewed, justified, version-bound, and non-writable to protected host state.
- Do not map the host repository read-write, the host virtual environment, host
  `.tradingagents`, host home, host temp, credential stores, production evidence
  roots, audit working directories, or any protected host root.
- The protected-root manifest includes the full
  `C:\Users\56922\.tradingagents` tree and every additional protected root
  declared by the immutable-state verifier. A protected root may not be silently
  omitted because it is missing, inaccessible, redirected, or a reparse point.
- If inspection input is required, supply only a prepared immutable,
  path-redacted control inventory or an exceptional reviewed read-only mapping;
  never expose the real protected root read-write.
- Assign all guest-writable environment/default locations explicitly so no
  guest default resolves to the host user profile or a mapped host path.

## Input and output identities

Inputs are the reviewed commit ID, reviewed tree hash, sealed source archive
SHA-256, source inventory digest, runner/bootstrap identity, `.wsb` config
identity, protected-root-manifest identity, mapping manifest, path-redacted
control-inventory identity, and runtime-bundle input identity when present.
These values are cross-checked before launch and are material to the run
identity.

The output contract is a bounded, machine-readable result set in one new empty
run-specific export folder. It binds every input identity, records validation
and mapping results, rejects undeclared files, and reserves a completion marker
that later execution tickets must write last. Runtime databases, protected-state
copies, credentials, raw provider/model material, and unbounded logs are not
valid outputs.

## Security rules

- Enumerate every repository-defined `RUN_LIVE_*` variable and force it to `0`;
  this includes DeepSeek, provider, and Tushare live opt-ins at minimum.
- Set `PYTHON_DOTENV_DISABLED=1` before project imports or test collection.
- Reject `.env`, `.env.enterprise`, API/provider/model credentials, cookies,
  credential stores, package-index tokens, credential-bearing URLs, and secret
  derivatives from every source, runner, control, runtime, and export input.
- Do not read or reuse the host venv, inject host credentials, enable networking,
  or treat Python/sitecustomize hooks as the containment authority.
- Treat the host protected inventory and final host verdict as trusted host
  responsibilities, never as guest-controlled output.

## Historical compatibility

- Preserve every existing baseline/inventory and all prior QCF17 incident
  evidence, including the four fake-Yahoo incidents and
  `isolated-python-write-probe`, under their existing identities.
- Preserve the persisted production Market Session Calendar, provider ledger
  and SQLite sidecars, checkpoints, reports/logs, and failed Ticket 07 evidence.
- Do not copy the QCF17 isolation prototype implementation into production. Its
  report is design evidence for the runner contract only.

## Deterministic regressions

- Prove canonical serialization and identity stability for the runner, config,
  source archive, protected manifest, mapping manifest, and result schema.
- Prove every forbidden writable mapping/default path fails closed and exactly
  one fresh result-export mapping is accepted.
- Prove clean reviewed tree acceptance and dirty/staged/untracked/reparse-point/
  source-hash mismatch rejection without running project code or hooks.
- Prove all guest-writable defaults resolve beneath guest-local roots and all
  live/dotenv controls are fixed before executable work.
- Prove secret and unsafe-export canaries are rejected with bounded,
  path-redacted diagnostics.

## Explicit non-goals

- Launching Ticket 06 or Ticket 07, running any QCF/PE/AC suite, or freezing a
  baseline.
- Executing Windows Sandbox qualification or claiming QCF17 containment PASS.
- Preparing/installing the offline dependency bundle owned by ISO-02.
- Mapping protected host state, a writable host repository, host venv, or host
  `.tradingagents`; enabling networking; live providers; paid models; or
  authoritative `qualified_v1`.
- Copying prototype code into production or weakening the selected Windows
  Sandbox OS boundary to Python-only guards, a host-side disposable copy, or an
  unqualified ACL/container substitute.

## Verification commands

Run only deterministic contract/static checks; do not launch Ticket 06:

```powershell
pytest -q -k "iso_01 or sandbox_runner_contract or sandbox_workspace_contract or bounded_result_export_contract"
ruff check tradingagents cli tests
git diff --check
```

The implementing agent must also run the launcher's no-launch input/config
validation mode and record its exact invocation and bounded output identity in
this ticket. A minimal Sandbox launch belongs to ISO-03, not ISO-01.

## Definition of done

- [ ] One reviewed versioned launcher/config and protected-root/mapping contract
  enforce the exact read-only inputs, sole writable result mapping, sealed clean
  source, and guest-local writable layout.
- [ ] Network, integration, live-test, dotenv, credential, and host-default
  controls fail closed and are identity-bound.
- [ ] Input/output identities and bounded export validation are deterministic,
  secret-free, and covered by regressions.
- [ ] Historical state/incidents remain untouched; no Ticket 06/07 command,
  Sandbox qualification, live I/O, baseline freeze, or activation occurred.
- [ ] The accepted runner/input contract is ready for ISO-02 and no broader
  containment claim is recorded.

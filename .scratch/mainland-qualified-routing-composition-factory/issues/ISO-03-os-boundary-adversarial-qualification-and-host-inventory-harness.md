# ISO-03 — OS boundary/adversarial qualification and host inventory harness

**Parent specification:** [Mainland qualified routing production composition factory](../spec.md)

**What to build:** Qualify the actual Windows Sandbox OS boundary and the
trusted host before/after inventory harness by running only the exact-path
adversarial write matrix and valid guest-local write controls in a fresh
Sandbox. The result must prove zero host delta before any baseline is frozen or
Ticket 06 is allowed to run.

**Blocked by:** ISO-01A PASS — Executable Ticket 06 Sandbox acceptance mode;
rebuild/reseal of source and runtime inputs as needed for the materially changed
runner identities; and the existing immutable-state verifier used as the host
semantic authority. ISO-01 and ISO-02 remain completed historical prerequisites.

**Status:** ready-for-agent

## Replacement qualification requirement — 2026-08-14

The prior ISO-03 qualification remains valid historical evidence for its exact
qualified identities, but it is not qualification for the runner after ISO-01A.
ISO-01A materially changes bootstrap dispatch, sealed runner contents, command
ledger, support files, and bounded export validation. This ticket therefore
requires a new actual Windows Sandbox qualification on the rebuilt/resealed
source, runtime bundle, runner, config, mapping, and control identities.

Preserve the previous ISO-03 result and all associated inventories and evidence
unchanged. A replacement ISO-03 PASS unlocks only one new additive
post-qualification baseline freeze. It does not reuse, replace, or mutate an old
baseline and does not itself authorize ISO-04.

## Observable behavior

- Host qualification verifies a supported Windows edition, enabled
  `Containers-DisposableClientVM` feature, virtualization/hypervisor readiness,
  applicable nested-virtualization requirements, and a successful minimal
  Windows Sandbox launch.
- The trusted host harness resolves one canonical protected-root manifest and
  inventories it immediately before launch and after Sandbox exit while
  protected writers remain quiescent.
- Inside one fresh Sandbox, probes target the exact protected host absolute
  paths using normal Python, `python -I`, `python -S`, `python -E`,
  multiprocessing `spawn`, PowerShell `Set-Content`, `cmd` output redirection,
  direct filesystem writes, and direct SQLite DDL/DML.
- Each protected target is absent from the guest namespace, or every attempted
  write is denied by the OS for an exceptional reviewed read-only mapping. A
  same-spelled guest-local object does not count as host access.
- Control probes successfully write to sandbox-local temp, TradingAgents
  runtime/data/cache/history/checkpoint/report/log/payload roots, ordinary
  files, and SQLite databases.
- IPv4 and IPv6 default routes are absent, live-test variables remain `0`, and
  no provider/model/package network access occurs.
- PASS requires exact equality of trusted host before/after byte and semantic
  inventories. Any append, delete, rewrite, new sidecar, or semantic delta is a
  failure and may not be repaired, waived, or rebaselined.
- ISO-03 runs boundary qualification only. It does not run any Ticket 06 or
  Ticket 07 command.

## Exact OS containment requirements

- Use the exact reviewed ISO-01 containment contract, the ISO-01A runner and
  sealed acceptance-support identities, and the rebuilt/resealed source and
  ISO-02 runtime-bundle identities with networking disabled and only the one
  bounded writable result-export mapping.
- The canonical protected set includes the full
  `C:\Users\56922\.tradingagents` tree; provider/coordinator ledger and SQLite
  sidecars; production Market Session Calendar state and payloads; production
  checkpoints; production reports/logs; historical Ticket 07 evidence; all
  prior baselines/inventories/incidents; `isolated-python-write-probe`; and
  every additional protected root declared by the immutable-state verifier.
- Missing, unresolved, inaccessible, unexpectedly absent, or reparse-point
  targets fail preflight rather than narrowing the manifest.
- Protected host roots, host home/temp/venv, credential stores, and host
  repository are not writable mappings. Prepared inspection input is immutable
  and path-redacted or explicitly reviewed read-only.
- L2 covers ordinary arbitrary user-mode descendants that know exact protected
  paths. Windows Sandbox/Hyper-V/kernel/driver/firmware escape, compromise of
  the trusted host inventory process, and administrator-level host compromise
  are excluded L3 threats.
- Python/sitecustomize behavior is secondary diagnostics only; the Stage-1
  `python -I` absolute-path probe remains mandatory.

## Input and output identities

Inputs bind the reviewed commit/tree/source archive, ISO-01 containment
contract, ISO-01A runner/bootstrap/sealed-runner identities and `.wsb` config,
protected-root and mapping manifests, path-redacted control inventory, rebuilt
ISO-02 bundle manifest/hash as needed, probe-matrix version, immutable-state
verifier version, and fresh export-directory identity.

The host before/after inventory binds every protected relative path, length,
SHA-256, SQLite main file/sidecar, provider sequence count/digest, provider
attempt count/digest, calendar revision/content, checkpoint/report/log
inventories, incident evidence, prior baseline/inventory evidence, and Ticket 07
evidence. The bounded output binds both host inventory identities, every
adversarial and valid-write outcome, network-route result, Sandbox exit, and a
completion marker written last.

## Security rules

- The guest has no credentials, `.env`, `.env.enterprise`, cookies, credential
  stores, provider/model keys, package-index tokens, or credentialed URLs.
- All `RUN_LIVE_*` values are `0`; `PYTHON_DOTENV_DISABLED=1`; no network route,
  live provider/model work, dependency download, or calendar refresh is
  allowed.
- The guest cannot author or replace the trusted host inventory or final
  comparison. Result-export content is bounded and independently validated.
- Reject unbounded logs, runtime provider databases, host state copies,
  credentials, or undeclared output even if the probes otherwise pass.
- Keep protected writers quiescent throughout the host before/after window.

## Historical compatibility

- Retain all four fake-Yahoo incidents, `isolated-python-write-probe`, every
  prior baseline/inventory, the persisted production calendar, and failed
  Ticket 07 material byte-for-byte under their existing identities.
- Retain the previous ISO-03 qualification and the failed ISO-04
  `qualification_invalidated` evidence under their existing identities. They
  remain historical only and are not rewritten as qualification for ISO-01A.
- Do not delete, repair, renumber, reinterpret, migrate, or rebaseline any
  protected material after a failed probe.
- Replacement ISO-03 may authorize one new additive post-qualification baseline
  only by passing; the baseline freeze is a separate next edge and does not
  occur in this ticket.

## Deterministic regressions

- Validate canonical protected-manifest resolution, reparse/missing-root
  failures, deterministic byte inventory, and independent semantic verifier
  results before launching the Sandbox.
- Exercise every adversarial process/startup mode against every representative
  protected path class and distinguish namespace absence, OS read-only denial,
  guest-local containment, and any forbidden host mutation.
- Exercise ordinary guest-local file, temp, TradingAgents root, SQLite DDL/DML,
  and payload writes as positive controls.
- Prove IPv4/IPv6 route absence and failure of network/provider/model attempts.
- Tamper host inventory, guest output, completion ordering, mappings, or input
  identities and prove qualification fails closed.

## Explicit non-goals

- Running Ticket 06, QCF1–QCF18, PE1–PE15, AC1–AC13, full pytest, Ticket 07,
  live providers, or paid models.
- Freezing a new baseline before ISO-03 PASS or modifying any old baseline after
  failure.
- Weakening QCF17 to Python-only hooks, a host-side disposable repository,
  unqualified ACLs, process-isolated containers, or a writable protected-state
  mapping.
- Claiming protection from L3 Sandbox/Hyper-V/kernel/administrator compromise.
- Enabling or recommending `qualified_v1`, or copying the QCF17 prototype
  implementation into production.

## Verification commands

Run the reviewed runner's boundary-qualification mode only. Its command ledger
must include the following host qualification and guest probe operations, with
no Ticket 06 selector:

```powershell
Get-WindowsOptionalFeature -Online -FeatureName Containers-DisposableClientVM
systeminfo
python <probe> normal
python -I <probe>
python -S <probe>
python -E <probe>
powershell -NoProfile -Command "Set-Content -LiteralPath <protected-target> -Value <canary>"
cmd /d /c "echo <canary> > <protected-target>"
pytest -q -k "iso_03 or sandbox_boundary_qualification or protected_host_inventory"
```

The versioned harness must also execute the multiprocessing-spawn, direct
filesystem, direct SQLite DDL/DML, guest-local positive controls, minimal
Sandbox launch, network-route checks, and trusted before/after comparison. The
implementing agent records the exact runner invocation and safe bounded output;
placeholders above are never expanded to a host write outside the qualified
Sandbox.

## Unsupported-host outcome

If Windows Sandbox cannot be qualified, set this ticket to `needs-info`, retain
the failure evidence, and use the separately specified full dedicated-VM
fallback with equivalent unmapped-host semantics. Do not weaken QCF17.

## Definition of done

- [ ] Windows edition/feature/hypervisor readiness and a minimal Sandbox launch
  are qualified on the actual host.
- [ ] Every adversarial exact-path probe is OS-denied or guest-local/absent,
  every valid guest-local write succeeds, and networking is absent.
- [ ] Trusted host byte and semantic inventories are exactly equal for every
  required protected object, provider/calendar counter/digest, incident, and
  Ticket 07 evidence item.
- [ ] No Ticket 06/07, live provider/model, baseline freeze, historical repair,
  or `qualified_v1` activation occurred.
- [ ] Replacement PASS evidence binds all new reviewed identities and unlocks
  only one new additive post-qualification baseline freeze. Unsupported hosts
  return `needs-info` for the dedicated-VM fallback.

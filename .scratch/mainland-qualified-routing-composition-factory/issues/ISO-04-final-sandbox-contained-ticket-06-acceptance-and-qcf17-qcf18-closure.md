# ISO-04 — Final Sandbox-contained Ticket 06 acceptance and QCF17/QCF18 closure

**Parent specification:** [Mainland qualified routing production composition factory](../spec.md)

**What to build:** Run every executable Ticket 06 command inside one qualified
Windows Sandbox runner on the exact reviewed identities, export only the bounded
acceptance result, and close QCF17 only when sandbox-local behavior and the
trusted zero-host-delta proof both pass. Evaluate QCF18 only after QCF17 PASS.

**Blocked by:** ISO-01A PASS — Executable Ticket 06 Sandbox acceptance mode;
replacement ISO-03 PASS on the materially changed identities; and one new
additive post-qualification baseline freeze. The baseline must bind the newly
qualified source, runtime bundle, runner/config, protected manifest, command
ledger, export contract, and host inventory identities.

**Status:** needs-triage

**Latest result:** `pass` — ISO-04 run `iso04-20260818-ticket06-003`;
Ticket 06 `FINAL PASS`

## Confirmed blocked attempt — 2026-08-14

**Result:** `qualification_invalidated`

The qualified ISO-03 runner could not execute Ticket 06 acceptance: its guest
bootstrap accepted only `boundary_qualification`, invoked only the boundary
qualification orchestrator, and used a contract requiring
`ticket06_executed=False`; its sealed runner manifest contained only
boundary-qualification files. Although `ticket06_acceptance` was reserved as an
enum value, there was no executable Ticket 06 acceptance runner, deterministic
command ledger, or bounded acceptance-export contract.

No Sandbox was launched, no Ticket 06 command ran, and no protected host state
changed. Preserve this failed ISO-04 evidence, the prior ISO-03 qualification,
and the existing baseline unchanged. The attempt may be retried only through
ISO-01A, replacement ISO-03 PASS, and the new additive post-qualification
baseline freeze.

## Observable behavior

- One qualified Sandbox invocation executes systemic persistence isolation;
  CAL-01 through CAL-04 deterministic suites; 06B temporal acceptance;
  QCF1–QCF18; PE1–PE15; AC1–AC13; live-test isolation; non-integration; two full
  suite passes; Ruff; diff/whitespace; and every executable Ticket 06 review or
  check.
- No executable Ticket 06 slice runs on the host. Host work is limited to
  reviewed source/bundle staging, protected before/after inventories, Sandbox
  launch/monitoring, bounded export validation, and the final verdict.
- QCF17 PASS is conjunctive: (A) sandbox-local acceptance and forward-only
  historical behavior are correct; and (B) the trusted host byte and semantic
  inventories are exactly identical after Sandbox exit.
- A contained Python/sitecustomize guard failure is diagnostic and does not
  itself prove host mutation. Any host append, delete, rewrite, new sidecar, or
  semantic delta is QCF17 FAIL and cannot be repaired, waived, or rebaselined.
- QCF18 evidence may be collected during the same invocation, but QCF18 remains
  conditionally unevaluable and cannot be credited until the host establishes
  QCF17 PASS.
- The completion marker is written last. Missing, mismatched, extra, reordered,
  secret-bearing, unbounded, or incomplete output fails acceptance.
- ISO-04 does not run Ticket 07, perform provider/model I/O, or enable
  `qualified_v1`.

## Exact OS containment requirements

- Use the replacement-ISO-03-qualified Windows Sandbox runner with the identical
  source, bundle, ISO-01A runner/bootstrap/command-ledger/export-contract,
  `.wsb` config, protected manifest, mapping manifest, and trusted inventory
  contracts bound by the new post-qualification baseline.
- Source, runtime bundle, runner, and path-redacted control inventory remain
  read-only inputs. Exactly one fresh empty bounded result-export folder is the
  sole writable host mapping.
- Protected roots, host `.tradingagents`, host repository, host venv, host
  profile/temp, credentials, and production evidence remain unmapped or reviewed
  read-only as qualified by ISO-03.
- The entire writable repository, Python environment, profile, temp, TradingAgents
  runtime/data/cache/history/checkpoint/report/log/payload roots, pytest temp,
  and Python caches remain guest-local.
- Networking, clipboard, and unnecessary integrations remain disabled/minimal;
  `ProtectedClient` remains enabled where qualified and supported.
- All `RUN_LIVE_*` variables remain `0` and
  `PYTHON_DOTENV_DISABLED=1` throughout bootstrap, collection, tests, reviews,
  and result export.

## Input and output identities

Inputs bind the reviewed commit/tree, sealed source hash, runtime-bundle
manifest/hash, ISO-01A runner/bootstrap and `.wsb` config identities,
protected-root and mapping-manifest identities, replacement ISO-03 adversarial
qualification identity, frozen post-qualification baseline identity,
command-ledger identity, bounded acceptance-export-contract identity, and fresh
result-export identity.

The bounded export must include the reviewed commit/tree, source hash, bundle
hash, runner/config identity, protected-manifest and baseline identities,
adversarial probe result, QCF1–QCF18 results, PE1–PE15 results, AC1–AC13 results,
pytest summaries, live-test isolation, Ruff result, diff/whitespace result,
executable review results, and a completion marker written last. It must not
export runtime provider databases, credentials, host production-state copies,
raw provider/model material, or unsafe unbounded logs.

## Security rules

- No `.env`, `.env.enterprise`, credentials, cookies, credential stores,
  package-index tokens, provider/model secrets, or credential-bearing URLs enter
  any input, guest environment, output, log, or identity.
- Networking is physically absent; no dependency install, provider call, paid
  model, calendar refresh, integration opt-in, or live qualification occurs.
- The guest result is evidence, not the final containment authority. The host
  independently computes and compares the protected before/after inventories.
- Protected writers remain quiescent across the inventory window. Any inventory
  mismatch is terminal for this run.
- Export validation is allowlist/bounds-based and rejects runtime databases,
  host-state copies, secrets, raw payloads/errors, and unbounded text.

## Historical compatibility

- Preserve the historical pre-temporal and post-temporal/pre-CAL Ticket 06
  evidence exactly as recorded. The new result is additive and does not relabel
  either historical PASS.
- Preserve all previous failed QCF17 incidents, including the four fake-Yahoo
  incidents and `isolated-python-write-probe`, all prior baselines/inventories,
  production calendar state, checkpoints/reports/logs, and failed Ticket 07
  evidence.
- New qualified material uses the exact forward contracts already accepted by
  Ticket 06; old material remains readable only under its recorded contracts.
- No failure may be repaired by deleting evidence, replacing a baseline,
  rewriting a run, or reusing an ID.

## Deterministic regressions

- Execute every Ticket 06 deterministic command from one reviewed guest-local
  tree and compare both complete-suite runs for the accepted deterministic
  result.
- Re-run systemic persistence isolation, the full CAL suites, every 06B temporal
  boundary, QCF1–QCF18, PE1–PE15, AC1–AC13, secret/history/live-isolation checks,
  and both review stages without waiver or renumbering.
- Prove sandbox-local runtime/temp/SQLite/payload writes succeed while the
  protected-path adversarial matrix remains OS-denied or guest-local.
- Prove the export rejects missing/duplicate/unknown IDs, identity mismatch,
  incomplete criteria, early/multiple completion markers, unbounded files,
  secrets, provider DBs, and host-state copies.
- Recompute the trusted host inventory after exit and prove zero append, delete,
  rewrite, provider sequence/attempt change, calendar change,
  checkpoint/report/log change, incident change, or Ticket 07 evidence change.

## Explicit non-goals

- Running any executable Ticket 06 slice on the host or splitting acceptance
  across host and Sandbox.
- Running Ticket 07, allocating its run/checkpoint ID, contacting providers or
  paid models, refreshing the production calendar, or granting live
  authorization.
- Enabling/recommending `qualified_v1`, changing QCF/PE/AC criteria, or waiving
  QCF18 after QCF17 failure.
- Replacing/freezing another baseline during the run, repairing historical
  evidence, or copying the QCF17 prototype implementation into production.
- Weakening the qualified OS boundary or treating Python guards/guest verdicts
  as the final containment authority.

## Verification commands

The qualified runner executes the existing Ticket 06 command ledger entirely
inside one guest. At minimum the ledger includes the systemic-isolation, CAL,
06B, QCF, PE, AC, live-test-isolation, history/security, and executable review
selectors plus these exact final commands:

```powershell
pytest -q -m "not integration"
pytest -q
pytest -q
ruff check tradingagents cli tests
git diff --check
```

The host runs no command above. It performs only input verification, protected
before/after inventory, Sandbox launch/monitoring, bounded export validation,
and final QCF17-then-QCF18 evaluation. The complete command ledger and each exit
code/count must appear in the bounded export.

## Exact dependency graph

```text
CAL-01 -> CAL-02 -> CAL-03 -> CAL-04 [complete]
CAL-04 -> production calendar bootstrap [complete]
production calendar readiness -> ISO-01
ISO-01 -> ISO-02
ISO-01 + ISO-02 + current ISO-03 findings -> ISO-01A
ISO-01A PASS -> rebuild/reseal source and runtime inputs as needed
new identities -> replacement ISO-03 actual Windows Sandbox qualification
replacement ISO-03 PASS -> freeze new additive post-qualification host baseline
new baseline freeze -> ISO-04
ISO-04 -> Ticket 06 PASS
Ticket 06 PASS -> fresh Ticket 07 authorization
fresh Ticket 07 authorization -> NEW 601328.SS
NEW 601328.SS PASS -> NEW 600895.SS
```

## Definition of done

- [x] Every executable Ticket 06 command ran inside one qualified Sandbox on
  the exact baseline-bound identities; no executable slice ran on the host.
- [x] The bounded export is complete, identity-consistent, secret-free, and
  ends with the completion marker written last.
- [x] QCF17 clause A passes and the trusted QCF17 clause B host inventory is
  exactly equal with zero byte or semantic delta.
- [x] Only after QCF17 PASS, QCF18 and every unchanged QCF/PE/AC/full-suite,
  Ruff, diff, and review criterion are credited PASS.
- [x] Historical evidence remains unchanged, no live provider/model/Ticket 07
  work occurred, and `qualified_v1` remains disabled.
- [x] ISO-04 records PASS and thereby permits Ticket 06 final PASS; Ticket 07
  remains blocked pending a separate fresh authorization.

## ISO-04 / Ticket 06 final attempt — 2026-08-17

### Final disposition

- **ISO-04:** `FAIL`.
- **Ticket 06:** remains `BLOCKED`; it is not FINAL PASS.
- **QCF17 clause A:** `FAIL`. Mandatory command 20 failed, so complete
  sandbox-local acceptance was not established.
- **QCF17 clause B:** `PASS`. The independent HOST AFTER inventory exactly
  equals the required HOST BEFORE identity with zero protected-state delta.
- **QCF17 overall:** `FAIL`, because clauses A and B are conjunctive.
- **QCF18:** `NOT EVALUATED`, because QCF17 did not pass.
- **Ticket 07:** not executed or authorized. No Ticket 07 run/checkpoint ID was
  allocated.

No baseline was frozen or replaced, ISO-03 was not rerun, ISO-02 was not
resealed, no historical/untracked evidence was cleaned, and no self-
requalification or second ISO-04 attempt was performed.

### Authoritative inputs and prelaunch gates

The run used one fresh Windows Sandbox with run ID
`iso04-20260817-ticket06-002` and external evidence root
`D:\TradingAgents-ISO04-Ticket06-Acceptance-Final-26ba602`.

| Binding | Verified value |
| --- | --- |
| Source commit | `26ba602f636e7e07dcb5966507257161f34948fa` |
| Source tree | `b4bb39c72f651fa8afd24bb395651a6ccc9273cb` |
| Frozen baseline | `qcf17-post-iso03-qualified-host-baseline:v3:sha256:60365097bc5e38c04c0c6bf3b8311de8b7a6f4a1acf7f1f44dafefe44fb455d9` |
| Frozen baseline artifact SHA-256 | `33fa9bd00b4b107bad46e7ee2408d11aac9e7536a261ecb396936a99a8fd0552` |
| Replacement ISO-03 run identity | `iso03-boundary-qualification-run:v1:sha256:61891cc6e30a397935b864f4f5c08ceb66692352dccd97695df6e31747e1d74c` |
| Replacement ISO-03 report identity | `iso03-host-qualification-report:v1:sha256:71b321ade480d51ba997a05cb7f49c2aaed6972137edc8c01aa9ae883dda68d9` |
| Replacement ISO-03 report SHA-256 | `fa8eb996837b67193570e0532b3641221cd706dea550738d4b19af34ffc7895c` |
| Runtime bundle | `windows-offline-runtime-bundle:v1:sha256:ea8e21a5de5b8f2bfa5e5dfd287381801f939603fcb78aba7c5dd7d060dd508d` |
| Qualified runner | `iso01-windows-sandbox-runner:v1:sha256:88768aba45c4b261702a875aa16926ab49fc52b17b2e2b6239fc8574f6fe4fe8` |
| Runner input | `windows-sandbox-qualified-runner-input:v2:sha256:ab3fa917e95fa56cfcd748cb3133bd5cc2f45174b6f391e8b5d6b4a6dfb294c3` |
| Exact command ledger | `ticket06-sandbox-command-ledger:v1:sha256:5444477692f8a4a517d6e78719c5d21d97550c6a08610bd88596ca76cb68ea23` |
| Protected-root manifest | `windows-sandbox-protected-roots:v3:sha256:ed99639d2053a7c0bd2f542941eda5cccd95b161fd81ad610cd1684a440fee62` |
| Immutable verifier | `ticket06-immutable-state-verifier:v1:sha256:65491e3ad63f298a5d0b9e7e6c1caf7f6a415827e22b15de81c0d6f4bd5287de` |
| Acceptance control | `windows-sandbox-ticket06-control:v1:sha256:4daf96d0f4e949f61f865166bc8692faef59edda79c5fe53bb87ba7234b90bae` |
| Acceptance runner | `iso01-windows-sandbox-runner:v1:sha256:5c973909f1a8dea6f302c4b4d884798cc99d21113fcca9bfa609a796ee50688b` |
| Acceptance `.wsb` config | `windows-sandbox-config:v1:sha256:08fa1cf777c103aff5f284cf8079286f375bbe415c117d90a58e68e53bbd5df6` |
| Acceptance mapping manifest | `windows-sandbox-mapping-manifest:v1:sha256:d69dd3db6edb60e92a87110e62a2f99cecbc895b661272b77183185e3e229995` |

Both the sentinel and immediate HOST BEFORE recomputations were exactly:

`windows-sandbox-protected-host-inventory:v1:sha256:c22a376428713499832c84acb1ae2a0e1d0f4863cba916fd4c3e0f50159fa4d4`

The prelaunch semantic gates also matched the frozen values: 1,024 provider
sequences (`9c5df0762c75bdc1941cd9ed1b321ad76a558d24eaa74810febd026688412d97`),
1,036 provider attempts
(`8f4c8301d746f8cd3831d77aaf26d6b0e552d272907dfe3e90b5ca43234b5a10`),
the one 2026 mainland calendar revision and its 365 sessions, three checkpoint
files, 3,104 report/log files, all four fake-Yahoo incident rows,
`isolated-python-write-probe`, and all five Ticket 07 evidence files.

### Containment and authorization evidence

- Networking was disabled. The guest evidence reports no usable network/live
  command and no provider or model command execution.
- `PYTHON_DOTENV_DISABLED=1`.
- `RUN_LIVE_CUSTOM_TESTS`, `RUN_LIVE_DEEPSEEK_TESTS`,
  `RUN_LIVE_ENVIRONMENT`, `RUN_LIVE_PROVIDER_TESTS`, and
  `RUN_LIVE_TUSHARE_TESTS` were all `0`.
- No credentials were present in the guest environment or bounded export.
- No production calendar refresh occurred.
- `qualified_v1` remained disabled.
- The source, runner, control, and runtime inputs were read-only; the run had
  exactly one writable host results mapping.

### Exact 27-command ledger result

The guest executed commands in sealed order and stopped after the first
mandatory failure, as required.

| # | Command ID | Status | Bounded summary |
| ---: | --- | --- | --- |
| 1 | `systemic_persistence_isolation` | PASS | 38 passed |
| 2 | `cal01_acquisition_attempts` | PASS | 44 passed |
| 3 | `cal02_publication_exact_reader` | PASS | 47 passed |
| 4 | `cal03_programmatic_cli_bootstrap` | PASS | 123 passed |
| 5 | `cal04_deterministic_acceptance` | PASS | 35 passed |
| 6 | `temporal_06b` | PASS | 62 passed |
| 7 | `qcf_matrix` | PASS | 19 passed |
| 8 | `composition_contracts` | PASS | 158 passed |
| 9 | `eligibility_context` | PASS | 108 passed |
| 10 | `production_source_bindings` | PASS | 79 passed |
| 11 | `indicator_metadata` | PASS | 63 passed |
| 12 | `provider_adapters` | PASS | 193 passed |
| 13 | `provider_coordination` | PASS | 137 passed |
| 14 | `graph_shadow_pe` | PASS | 209 passed |
| 15 | `checkpoint_reporting_evidence` | PASS | 171 passed |
| 16 | `history_security_checks` | PASS | 19 passed |
| 17 | `market_history_ac` | PASS | 146 passed |
| 18 | `financial_period_pe` | PASS | 73 passed |
| 19 | `live_test_isolation` | PASS | 9 passed |
| 20 | `non_integration` | **FAIL** | exit 1; 3,437 passed, 1 failed, 13 skipped |
| 21 | `full_suite_a` | NOT RUN | stopped after mandatory failure |
| 22 | `full_suite_b` | NOT RUN | stopped after mandatory failure |
| 23 | `ruff` | NOT RUN | stopped after mandatory failure |
| 24 | `diff_whitespace` | NOT RUN | stopped after mandatory failure |
| 25 | `workspace_status` | NOT RUN | stopped after mandatory failure |
| 26 | `offline_spec_compliance_review` | NOT RUN | stopped after mandatory failure |
| 27 | `offline_code_quality_review` | NOT RUN | stopped after mandatory failure |

The bounded contract intentionally exports only aggregate pytest counts, not
the failing test name or raw failure text. The failed command result identity
is
`ticket06-command-result:v1:sha256:f992e1d1e86c882fe63bd5de3ddbae84f32b09f0d6ebe4329da344310e0c4ce7`.

Commands 1–19 establish PASS evidence for QCF1–QCF16, PE1–PE15, and AC1–AC13.
Command 20 makes the overall guest claim `fail`, QCF17 guest clause A `fail`,
and QCF18 guest evidence `fail`; the latter is not credited or evaluated by the
host because final QCF17 failed.

### Sandbox exit and bounded-export validation

The Sandbox terminated normally without timeout. Durable process evidence
records outer return code `0`, `timed_out=false`, and
`termination_confirmed=true`. The guest result correctly records `guest_claim`
`fail`. The host launch seam therefore raised `result_identity_mismatch`
because it requires the outer return-code success bit to equal the guest-claim
success bit.

The unchanged, preserved bounded export subsequently validates against the
sealed acceptance control: its declared result identity recomputes exactly and
its static bindings match the control. This post-exit validation does not
change or waive the command-20 failure.

| Evidence | Identity / SHA-256 |
| --- | --- |
| Guest result | `windows-sandbox-ticket06-result:v1:sha256:5b63c739e82c1310c3cc712678cad5015deb04db0212593988e24d2b4b6bcf1f` / `c5424f36930afc69032fe589c3cf34b748fb963f54d0e7587bc2a3624b65458a` |
| Completion marker | `windows-sandbox-ticket06-completion:v1:sha256:b0d2f6c335ea934220a5a8976f1abecef6718957efcf0e8e87f166176c34e400` / `8424cea1f348529f5bed137866318929069333c3f62c175c1b0c812ab68dd4df` |
| Guest export | `windows-sandbox-ticket06-result-export:v1:sha256:0b1a90249dc18717598b402edc0290111bb86b8b59c907870ea0cc5f47d8710c` |
| Sandbox process evidence | `iso04-sandbox-process-evidence:v1:sha256:9de2f65373d70662045e8fd515fe8f240513d55e1d0094e1fd8cb614990ce209` / `37d6dfbbd4c802e80547d4241838900349e5d88355cc6f66e26e992ac7da1b2e` |
| Operational evidence | `iso04-ticket06-operational-evidence:v1:sha256:39b0c0862031514ecc1e9a928c16d9f7fd13f4521b7284d8577d6cd5e2e6c54b` / `9a0bb953ad7186e15b3002bf867eeb6ade1941a3dd9a0ba6df296c579e0e1f28` |
| Acceptance report | `iso04-ticket06-acceptance-report:v1:sha256:133b6b51fb05dcc207bc7ecbf99e752d19da6621f8ccd9a9f1a29c030da4a174` / `c682dfe4f8ff2a0a5a9222c3e2d932413f3eed716222c0706bc0f008af1680e5` |

### Independent HOST AFTER / QCF17 clause B

The independently recomputed HOST AFTER identity is exactly:

`windows-sandbox-protected-host-inventory:v1:sha256:c22a376428713499832c84acb1ae2a0e1d0f4863cba916fd4c3e0f50159fa4d4`

HOST BEFORE and HOST AFTER artifacts are byte-identical with SHA-256
`950dfccb7754f56037a975e658a723207570ec280d25376a3ba7a94ac8c1f4be`.
The comparison records zero appended, deleted, or rewritten files; no changed
roles; equal protected manifest and immutable verifier; and exact semantic
equality. Its delta identity is
`windows-sandbox-host-inventory-delta:v1:sha256:5c40296e1c7c63d51ab2bce7d2cc2db3ecf2ee38a5f032a482f2f7bcf859a3e9`
(artifact SHA-256
`8b81a221218904fdbb0a1e9c8389674977ab44f81137b279d8e3ca2722c50e6c`).

This establishes QCF17 clause B PASS, including unchanged provider sequences
and attempts, calendar/checkpoints/reports/logs, incidents, prior evidence, and
Ticket 07 evidence. It cannot make QCF17 pass because clause A failed.

## ISO-04 / Ticket 06 successful retry — 2026-08-18

### Final disposition

- **ISO-04:** `PASS`.
- **Ticket 06:** `FINAL PASS`.
- **QCF17 clause A:** `PASS` — all 27 mandatory guest commands passed and the
  guest reported the unchanged forward-only historical behavior and offline
  acceptance contract satisfied.
- **QCF17 clause B:** `PASS` — independent trusted HOST BEFORE and HOST AFTER
  inventories are exactly equal with zero append, delete, rewrite, changed
  role, or semantic delta.
- **QCF17 overall:** `PASS`, because both mandatory clauses passed.
- **QCF18:** `PASS`, evaluated and credited only after QCF17 PASS. All unchanged
  QCF1-QCF18, PE1-PE15, AC1-AC13, full-suite, Ruff, whitespace/diff, workspace,
  and two-stage offline review evidence passed.
- **Ticket 07:** not executed or authorized. No Ticket 07 run/checkpoint ID was
  allocated.

ISO-03 was not rerun, ISO-02 was not resealed, no baseline was frozen or
replaced, no historical or untracked evidence was cleaned, and no
self-qualification or second ISO-04 Sandbox was performed.

### Authoritative inputs and prelaunch gates

The retry used exactly one fresh Windows Sandbox with run ID
`iso04-20260818-ticket06-003` and external evidence root
`D:\TradingAgents-ISO04-Ticket06-Acceptance-862a268`.

| Binding | Verified value |
| --- | --- |
| Source commit | `862a268b9cc1c08a3ebc59bbf1a74550ec878d75` |
| Source tree | `0a2944d43ed4e8b91628fe2d2f927cf70b66a15c` |
| Reviewed source | `windows-sandbox-reviewed-source:v1:sha256:e13802cbb85bc3f7a7b129dfbd9e79699ba9d122ef994f1d0206151862e961a0` |
| Source manifest | `windows-sandbox-source-manifest:v1:sha256:319cc9a6b21de117526a7b3597c4d6bd1668dbb24289def8f8064fcc5fdba29c` |
| Source archive SHA-256 | `52229bed710fb2980efeb8f5b0c9b4e154f3cc5de4cc1754943bb3bd69a13a1e` |
| Runtime bundle | `windows-offline-runtime-bundle:v1:sha256:7e66684c10c734e51f6aac72cd2bf7ece667c867ee7bd0d50aab9a2f8d828582` |
| Runtime manifest SHA-256 | `9bf01d4a4c800fa4b653db0ee37e10fddce6c1e469664360be4f1a373636099f` |
| Runtime input contract | `windows-sandbox-runtime-bundle-input:v1:sha256:8f7d33144fdf9ffa4f24b38540c57d75a3d83b834873ae9cbdfb8d445157ee1f` |
| Sealed runner input | `windows-sandbox-qualified-runner-input:v2:sha256:2781c2b8d072574e04a8b506e20676eff5c41297a222b69c8542745bec836322` |
| Exact command ledger | `ticket06-sandbox-command-ledger:v1:sha256:8a2cb9a483e2c68dbef2b154c8c22634878c159a8a78ad0c0c7d8776570e85ea` |
| Replacement ISO-03 run | `iso03-boundary-qualification-run:v1:sha256:e561c8c9f46213b6d628173e8a8357375975ce90b13a51ec75070b183e2330c8` |
| Replacement ISO-03 report | `iso03-host-qualification-report:v1:sha256:58cd477d0e15dc2ce6cb3e609457271e7dd35e2292e361e90cb709d62bdb030d` |
| Frozen baseline | `qcf17-post-iso03-qualified-host-baseline:v4:sha256:a7402ab00bf9d15f0cb1c8bda681198e84bd272d38c2cd8e91ec7625c976ef53` |
| Frozen baseline artifact SHA-256 | `5d27d5235b554bc07b604256c9090e53fdf60b7dc4eb69e323d0e155664ec626` |
| Protected-root manifest | `windows-sandbox-protected-roots:v3:sha256:ed99639d2053a7c0bd2f542941eda5cccd95b161fd81ad610cd1684a440fee62` |
| Immutable verifier | `ticket06-immutable-state-verifier:v1:sha256:65491e3ad63f298a5d0b9e7e6c1caf7f6a415827e22b15de81c0d6f4bd5287de` |
| Acceptance control | `windows-sandbox-ticket06-control:v1:sha256:b8ad3c1fba31fe2fa4d0cbd52e4b5dfd5b55eef6fca804385ebdcb691d01921a` |
| Acceptance runner | `iso01-windows-sandbox-runner:v1:sha256:73f01e029795e5ed751c7f3866abcb01da3bcaf2feb73348473c971bc6fc4a4d` |
| Acceptance `.wsb` config | `windows-sandbox-config:v1:sha256:952dbd88db4665e7d9fb721ec5a75bf5a4e2092706bf5f2025d20f4637be4fbc` |
| Acceptance mapping manifest | `windows-sandbox-mapping-manifest:v1:sha256:0f6f7a9256a1387d1908eff32e9bb5104526519585984565cf7d69ad0bbd528f` |
| Environment policy | `windows-sandbox-environment-policy:v1:sha256:8428ed3a9e8435cace5541ded703613d97ac25df2e08a163b94fb6d90e18a81f` |

The separately recomputed preliminary inventory and both in-run prelaunch
inventories were exactly:

`windows-sandbox-protected-host-inventory:v1:sha256:d8eab715effa7706985cfd2243eb7c3e32f87927754873f454c6370dade063e9`

This is the required post-freeze HOST identity. The historical run-022
pre-freeze identity `c22a3764...` was not used as HOST BEFORE. Prelaunch
semantics were exactly 1,024 provider sequences, 1,036 provider attempts, one
persisted 2026 Market Session Calendar revision with 365 sessions, three
checkpoint files, 3,104 report/log files, all four fake-Yahoo incident rows,
`isolated-python-write-probe`, and all five Ticket 07 evidence files.

### Containment and authorization evidence

- The `.wsb` disabled networking, vGPU, audio/video input, printer and
  clipboard redirection, and enabled `ProtectedClient`.
- Source, runtime, runner, and control were read-only mappings. The fresh empty
  bounded results export was the only writable host mapping.
- `PYTHON_DOTENV_DISABLED=1`.
- `RUN_LIVE_CUSTOM_TESTS`, `RUN_LIVE_DEEPSEEK_TESTS`,
  `RUN_LIVE_ENVIRONMENT`, `RUN_LIVE_PROVIDER_TESTS`, and
  `RUN_LIVE_TUSHARE_TESTS` were all `0`.
- No credentials entered the guest or bounded export. No provider command,
  model command, dependency download, production calendar refresh, or Ticket 07
  command executed.
- The replacement ISO-03 authority retained all nine contained adversarial
  exact-path probes, all eleven successful guest-local write controls, and no
  IPv4/IPv6 default route.
- `qualified_v1` remained disabled.

The read-only input inventories were byte-identical before launch and after
Sandbox exit:

| Input | Files | Bytes | Before/after inventory SHA-256 |
| --- | ---: | ---: | --- |
| Source | 3 | 15,596,997 | `f0f0d848bce10e032313f86640c317c5743f71c28c8c709245be8c4bf4608def` |
| Runner | 8 | 185,030 | `abe8b777c2b8b3e8a0285515db7c94a126b0f068a7b819509e7dd25476b944e4` |
| Control | 1 | 37,996 | `ec985bd1530ff0ccc41e0180b1ee1c7d096b35e1e58cea0b81b9955b78f1857d` |
| Runtime | 488 | 308,841,232 | `065cd134d523b7c5e46155a26982e1d207b3124bedcc9154c9af572ebfef4dda` |

### Exact 27-command ledger result

All commands ran inside the one guest, in the sealed order, and returned zero.

| # | Command ID | Status | Bounded summary |
| ---: | --- | --- | --- |
| 1 | `systemic_persistence_isolation` | PASS | 38 passed |
| 2 | `cal01_acquisition_attempts` | PASS | 44 passed |
| 3 | `cal02_publication_exact_reader` | PASS | 47 passed |
| 4 | `cal03_programmatic_cli_bootstrap` | PASS | 123 passed |
| 5 | `cal04_deterministic_acceptance` | PASS | 35 passed |
| 6 | `temporal_06b` | PASS | 62 passed |
| 7 | `qcf_matrix` | PASS | 19 passed |
| 8 | `composition_contracts` | PASS | 158 passed |
| 9 | `eligibility_context` | PASS | 108 passed |
| 10 | `production_source_bindings` | PASS | 79 passed |
| 11 | `indicator_metadata` | PASS | 63 passed |
| 12 | `provider_adapters` | PASS | 193 passed |
| 13 | `provider_coordination` | PASS | 137 passed |
| 14 | `graph_shadow_pe` | PASS | 209 passed |
| 15 | `checkpoint_reporting_evidence` | PASS | 171 passed |
| 16 | `history_security_checks` | PASS | 19 passed |
| 17 | `market_history_ac` | PASS | 146 passed |
| 18 | `financial_period_pe` | PASS | 73 passed |
| 19 | `live_test_isolation` | PASS | 9 passed |
| 20 | `non_integration` | PASS | 3,455 passed, 13 skipped |
| 21 | `full_suite_a` | PASS | 3,460 passed, 15 skipped |
| 22 | `full_suite_b` | PASS | 3,460 passed, 15 skipped |
| 23 | `ruff` | PASS | exit 0 |
| 24 | `diff_whitespace` | PASS | exit 0 |
| 25 | `workspace_status` | PASS | exit 0 |
| 26 | `offline_spec_compliance_review` | PASS | exit 0 |
| 27 | `offline_code_quality_review` | PASS | exit 0 |

The bounded export reports all QCF1-QCF18, PE1-PE15, and AC1-AC13 criterion
rows PASS without waiver or renumbering. Both offline review stages passed.

### Sandbox exit and bounded export

The Sandbox outer process returned `0`, did not time out, terminated cleanly,
and left no Sandbox process. The bounded result contains exactly `result.json`
followed by the completion marker written last.

| Evidence | Identity | Artifact SHA-256 |
| --- | --- | --- |
| Guest result | `windows-sandbox-ticket06-result:v1:sha256:51d716ed5a03da5d0f782e1d13d77f04c74c417c6188c6273fdc8a2677d164ae` | `0a706f0058b982366fddb7d5cd76ed1177f87d0db73c56a430a67183beda2635` |
| Completion marker | `windows-sandbox-ticket06-completion:v1:sha256:eae3a991f5e0546751df8e0a5af864c3d9157734f05527241d327bfdfb2b8a6b` | `a7d1bf2edf88325ac66b3391866f4a2810a32e5eead196dd5d08dabc5c0e0f03` |
| Guest export | `windows-sandbox-ticket06-result-export:v1:sha256:3f728fd963bfbd8def8b746f9d27db42ca1d53711e88b1e805f1a7c65f761ca5` | bounded two-file contract |
| Sandbox process evidence | `iso04-sandbox-process-evidence:v1:sha256:0e7ace3ab5c02451d91c180418d235e9552df0dc43da25524836108350d8c265` | `f978e30d2475865b186668cb7613e692a26ee792ffa25025f76f1e53f035bece` |
| Operational evidence | embedded by the acceptance report | `19f06699f3d3153420866d6c97bdeea35b702407f5c3a2b1ee700b5d3b1a114c` |
| Acceptance report | `iso04-ticket06-acceptance-report:v1:sha256:ca1e139ae8c88385ede59fb5c69aa008850696567d04988f63d792f52128cbdf` | `bc5119140da8fb3ba24db98a5d2a168871ad74ba786b483b709cb86e2550ae45` |
| Final verdict | `iso04-ticket06-final-verdict-v1` | `3e7e16cadd46b13161b7906cee3e20b6fa42f69c3aba78ffeeea3f451abfb162` |

After all Sandbox and host-inventory authorities were durably written, the
initial host verdict renderer treated the validated string `guest_claim` as an
enum and raised `AttributeError`. This post-run rendering incident changed no
guest result, completion marker, process evidence, host inventory, or input.
The existing evidence was reparsed and the final verdict completed host-only.
The Sandbox was not relaunched.

### Independent HOST AFTER and QCF17 clause B

The independently recomputed HOST AFTER identity is exactly:

`windows-sandbox-protected-host-inventory:v1:sha256:d8eab715effa7706985cfd2243eb7c3e32f87927754873f454c6370dade063e9`

HOST BEFORE and HOST AFTER artifacts are byte-identical, each 30,963,036 bytes
with SHA-256
`ac2ba85f0e938dd6c0b348fc102011900ea9a2090edf40ccf8be9ac8dce38e27`.
The comparison records appended/deleted/rewritten `0/0/0`, no changed roles,
equal protected manifest and immutable verifier, and exact semantic equality.
Its delta identity is
`windows-sandbox-host-inventory-delta:v1:sha256:0e7a6fb7b9110becae2331bc5275f4af47001551948eb260cfa1c6191b9ae33d`
(artifact SHA-256
`f0aaa0bf6cd43f6882566e4d502e2275a8fb2c722bfc7f8aab943e09838705bc`).

This proves unchanged provider sequences/attempts, Market Session Calendar,
checkpoints, reports/logs, incidents, baselines/prior evidence, and Ticket 07
evidence. QCF17 clause B therefore passes. With guest clause A already PASS,
QCF17 passes; only then was QCF18 evaluated and credited PASS. ISO-04 is PASS,
Ticket 06 is FINAL PASS, and Ticket 07 remains blocked pending a separate fresh
authorization.

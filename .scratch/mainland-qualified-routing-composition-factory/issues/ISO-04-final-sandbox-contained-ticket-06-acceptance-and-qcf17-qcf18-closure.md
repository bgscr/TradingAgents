# ISO-04 — Final Sandbox-contained Ticket 06 acceptance and QCF17/QCF18 closure

**Parent specification:** [Mainland qualified routing production composition factory](../spec.md)

**What to build:** Run every executable Ticket 06 command inside one qualified
Windows Sandbox runner on the exact reviewed identities, export only the bounded
acceptance result, and close QCF17 only when sandbox-local behavior and the
trusted zero-host-delta proof both pass. Evaluate QCF18 only after QCF17 PASS.

**Blocked by:** ISO-03 PASS; then the new additive post-incident host baseline
freeze. The baseline must bind the qualified source, runtime bundle,
runner/config, protected manifest, and host inventory identities.

**Status:** ready-for-agent

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

- Use the ISO-03-qualified Windows Sandbox runner with the identical source,
  bundle, runner/bootstrap, `.wsb` config, protected manifest, mapping manifest,
  and trusted inventory contracts bound by the new baseline.
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
manifest/hash, runner/bootstrap and `.wsb` config identities, protected-root and
mapping-manifest identities, ISO-03 adversarial qualification identity, frozen
post-incident baseline identity, command-ledger identity, and fresh result-export
identity.

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
ISO-01 -> ISO-02 -> ISO-03
ISO-03 PASS -> freeze new additive post-incident host baseline
baseline freeze -> ISO-04
ISO-04 -> Ticket 06 PASS
Ticket 06 PASS -> fresh Ticket 07 authorization
fresh Ticket 07 authorization -> NEW 601328.SS
NEW 601328.SS PASS -> NEW 600895.SS
```

## Definition of done

- [ ] Every executable Ticket 06 command ran inside one qualified Sandbox on
  the exact baseline-bound identities; no executable slice ran on the host.
- [ ] The bounded export is complete, identity-consistent, secret-free, and
  ends with the completion marker written last.
- [ ] QCF17 clause A passes and the trusted QCF17 clause B host inventory is
  exactly equal with zero byte or semantic delta.
- [ ] Only after QCF17 PASS, QCF18 and every unchanged QCF/PE/AC/full-suite,
  Ruff, diff, and review criterion are credited PASS.
- [ ] Historical evidence remains unchanged, no live provider/model/Ticket 07
  work occurred, and `qualified_v1` remains disabled.
- [ ] ISO-04 records PASS and thereby permits Ticket 06 final PASS; Ticket 07
  remains blocked pending a separate fresh authorization.

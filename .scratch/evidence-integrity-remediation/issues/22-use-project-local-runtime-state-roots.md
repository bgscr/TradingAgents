# Use project-local runtime state roots

Status: completed

## Problem

The development path in `start_tradingagents.ps1` does not establish
project-local runtime paths. With no explicit overrides, `DEFAULT_CONFIG`
therefore writes run output, cached data, checkpoints, and Trading Memory under
`~/.tradingagents`.

`results_dir` is not merely a text-log directory. It owns message/tool logs,
run status, reports, runtime metrics, content-addressed runtime artifacts,
decision audits, and graph state logs. The portable launcher already keeps its
output release-local, but it uses `reports/runs` and creates an otherwise unused
`logs` directory.

`data_cache_dir` owns reusable market-data CSVs, China A-share enhancement
responses, optional checkpoint databases, and picker PIT state.
`memory_log_path` owns `trading_memory.md`, which is created only after an
audited Trading Decision is authorized and later stores realized outcomes and
reflections.

## Settled direction

Runs started through the repository `start_tradingagents.ps1` development path
default to these separate roots:

- `<project>/logs` for the complete run bundle;
- `<project>/data/cache` for reusable cache, checkpoint, and PIT state; and
- `<project>/data/memory/trading_memory.md` for Trading Memory.

Explicit `TRADINGAGENTS_RESULTS_DIR`, `TRADINGAGENTS_CACHE_DIR`, and
`TRADINGAGENTS_MEMORY_LOG_PATH` values, including values loaded from the project
`.env`, win. Direct installed-library or CLI use outside the project retains
the user-home defaults.

The one-time migration of existing local history is an operator action, not an
automatic application migration.

## Acceptance

- Development `start_tradingagents.ps1 -DryRun` reports all three effective
  project-local runtime paths.
- A development launcher smoke run writes message/tool logs, run status,
  reports, metrics, runtime artifacts, decision audits, and graph state logs
  only beneath `<project>/logs`.
- Market-data and China-enhancement caches are written under
  `<project>/data/cache`; checkpoint and PIT state use that same configured
  cache root.
- Trading Memory is written only to
  `<project>/data/memory/trading_memory.md` and only through the existing
  audited-decision authorization boundary.
- Caller-supplied values for each of the three environment variables remain
  authoritative.
- Project `.env` overrides remain authoritative and are not masked by launcher
  initialization order.
- Direct use outside the repository continues to default to
  `~/.tradingagents`.
- `/logs/`, `/data/cache/`, and `/data/memory/` are ignored by Git, while
  tracked fixtures remain elsewhere.
- Portable-release paths either retain their documented behavior or are changed
  only with matching launcher, packaging, and documentation tests.
- The launcher does not automatically copy, delete, or reinterpret historical
  run artifacts.

## Implementation boundaries

- Primary files: `start_tradingagents.ps1`, `tradingagents/default_config.py`,
  `.env.example`, `.gitignore`, launcher tests, and Windows runtime-location
  documentation.
- Keep each runtime-root selection in the existing configuration path; do not
  add a second writer or per-call path override.
- Preserve the semantic separation between run output, reusable cache, and
  Trading Memory even though all three are project-local.
- Do not rewrite stale historical `run_status.json` files during path migration.
- Runtime-artifact garbage-collection recovery for abandoned runs remains
  outside this ticket.

## Required tests

- Development launcher defaults from a working directory outside the
  repository.
- Explicit process-environment overrides for all three paths.
- Project `.env` overrides and import-order regression for all three paths.
- Portable-launcher compatibility.
- Git-ignore assertion or equivalent repository hygiene check.
- One smoke assertion covering the complete per-run bundle, not only
  `message_tool.log`.
- Cache, checkpoint/PIT, and authorized Trading Memory write-path assertions.

## Comments

On 2026-07-22, 1,997 files totaling 32,664,990 bytes were moved from the local
user-home log root into `<project>/logs`. A relative-path, length, and SHA-256
manifest comparison reported zero differences, and the source directory was
left empty. Three preserved historical run-status files still say `running`,
but no TradingAgents process was active; they were deliberately not rewritten.

The same day, 185 cache files totaling 5,482,820 bytes were moved into
`<project>/data/cache` with zero relative-path, length, or SHA-256 differences.
The user-home cache and memory directories were left empty. Trading Memory had
no existing file to migrate.

## Resolution

- The repository launcher now supplies a project-runtime marker before importing
  the application. The existing configuration path derives `logs`,
  `data/cache`, and `data/memory/trading_memory.md` defaults from that marker.
- Development dry-run resolves configuration through the same Python import path
  as a real launch, so caller environment values and project `.env` values use
  the established precedence instead of being masked by launcher defaults.
- Direct installed-library and CLI use without the repository launcher retains
  the user-home defaults.
- Portable release defaults remain release-local, while explicit process
  overrides are now preserved.
- Git ignores the three project-local runtime roots, and operator documentation
  describes the development, installed, and portable behaviors.
- The launcher performs no automatic history copy, deletion, or rewrite.

Verification:

- Development dry-run from outside the repository reports the three intended
  project-local paths.
- Process-environment, project `.env`, portable-override, direct-use, and
  Git-ignore regressions pass.
- Checkpoint/PIT, complete run-bundle, runtime-artifact, report/audit, and
  authorized Trading Memory write-path suites pass.
- 192 targeted tests passed; Ruff and `git diff --check` passed.

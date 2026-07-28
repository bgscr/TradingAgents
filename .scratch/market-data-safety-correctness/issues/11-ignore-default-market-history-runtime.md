# 11 — Ignore only the generated default market-history runtime tree

Status: ready-for-agent

Blocked by: None — can start immediately

## Parent specification

[Market-data trading safety and point-in-time correctness](../spec.md). Its cross-cutting invariants, compatibility requirements, acceptance definitions, and Out of Scope section are binding.

## Problem being solved

A normal default-path run can create Market History Database files, payloads, sidecars, backups, and temporary maintenance state that make a clean worktree appear dirty. Git must ignore exactly the generated default runtime tree without hiding intentional fixtures, configuration, reports, source data, or unrelated directories.

## What to build

Add repository-root-anchored ignore rules for the configured default generated market-history runtime tree.

- Cover the main SQLite Market History Database, Ticket 05's derived payload-mutation sidecar lock database, WAL/SHM or journal sidecars, content-addressed payload tree, backups, atomic-install/backup temporary artifacts, and coordinator/maintenance state inside that exact default tree.
- Keep intentional source fixtures, configuration, audit/reports under existing policy, logs under existing policy, and market data outside the exact default tree visible to Git.
- Add only narrow exceptions for any intentionally tracked placeholder/documentation already inside the runtime tree.
- Verify a default-path CLI smoke leaves Git status unchanged relative to its captured pre-run baseline.

## Explicit non-goals

- Do not broadly ignore the entire `data` area or another parent directory.
- Do not delete, move, clean, or untrack any existing file.
- Do not hide source fixtures, registry/configuration files, reports, or negative-control paths.
- Do not change runtime path defaults or refactor configuration.

## Blocking dependencies

None. The parent specification defines the sidecar artifact and canonical default runtime tree sufficiently for independent implementation. Ticket 12 verifies the landed Ticket 05 output against these rules. This ticket blocks Ticket 12.

## External behavior and audit contract

- After a default-path CLI scenario, generated database, journal/sidecar, payload, backup, and temporary maintenance state does not appear as new Git status entries.
- Intentional audit/report output continues to follow existing rules and is not silently hidden.
- Negative-control fixtures, configuration, source files, and similarly named paths outside the anchored runtime tree remain visible.
- Already tracked files remain tracked; ignore behavior alone causes no data mutation.

## Acceptance evidence

- `git check-ignore -v` output for every required generated path and for negative controls that must remain visible.
- Captured Git-status baseline and post-smoke status showing no new generated runtime entries.
- Tracked-file listing proving ignore changes did not remove or untrack existing material.

## Regression tests

- [ ] `git check-ignore` recognizes the default main database, WAL/SHM, payload-mutation sidecar/journal, payload, backup, and temporary paths.
- [ ] Negative controls immediately outside the exact tree and intentional fixture/config/report paths remain unignored.
- [ ] A clean-worktree baseline before/after a deterministic default-path CLI smoke is identical except for intentionally governed audit output.
- [ ] Existing tracked files remain tracked and are not removed.
- [ ] Path matching is repository-root anchored and does not hide unrelated nested directories with similar names.

## Compatibility or migration requirements

- Ignore changes affect Git visibility for untracked generated state only.
- Do not delete, relocate, rewrite, or untrack existing runtime databases, payloads, reports, or source files.
- Preserve pre-existing user changes when comparing the before/after smoke baseline.

## Acceptance criteria from the specification

- AC12 — exact runtime-tree ignore behavior and clean-worktree smoke.
- AC13 — focused/full-suite, lint, diff validation, and no-unrelated-refactoring requirements.

## Verification commands

```powershell
pytest -q tests/test_windows_launchers.py -k gitignored
pytest -q -k "market_history and gitignore"
pytest -q
ruff check .
git diff --check
```

## Definition of done

- [ ] Only generated state inside the configured default runtime tree is ignored.
- [ ] The main database, payload-mutation sidecar, journals, payloads, backups, and temporary maintenance files are covered.
- [ ] Negative controls and tracked/intentional files remain visible and untouched.
- [ ] Default-path CLI smoke leaves the captured Git-status baseline unchanged as specified.
- [ ] Focused tests, the full supported suite, Ruff, and diff validation pass.

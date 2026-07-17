---
status: in-progress
branch: codex/recent-audit-20260714
timestamp: 2026-07-16T19:16:43-07:00
files_modified:
  - docs/2026-07-16-recent-code-reports-logs-audit.md
---

# Recent code, reports, and run-log audit

## Purpose

This file preserves the findings from a read-only audit of the latest code merge,
the newest generated trading reports, and the canonical runtime logs. The audit is
complete; implementation of the recommended fixes has not started.

## Repository state

- Primary repository: `D:\prj\TradingAgents\TradingAgents`
- Audit worktree: `D:\prj\TradingAgents\TradingAgents\.worktrees\agent\recent-audit-20260714`
- Audit branch: `codex/recent-audit-20260714`
- Reviewed merge: `41624b4` (`Merge branch 'codex/multi-horizon-picker-design'`)
- First-parent comparison: `fb67d3b..41624b4`
- Merge size: 33 changed files, 8,317 insertions, 53 deletions
- No source code was modified during the audit.
- CodeGraph was initialized and current in the worktree: 183 files, 3,092 nodes,
  and 7,288 edges.

The merge adds the Phase-1 point-in-time A-share data foundation under
`tradingagents/picker/` and serializes BaoStock sessions. Ranking, execution
simulation, corporate-action accounting, and walk-forward evaluation are
explicit later-phase scope, so their absence is not considered a merge defect.

## Artifacts reviewed

Newest copied reports:

- `D:\prj\TradingAgents\TradingAgents\reports\000725.SZ_20260714_092642\complete_report.md`
- `D:\prj\TradingAgents\TradingAgents\reports\000021.SZ_20260714_090246\complete_report.md`

Canonical run artifacts are stored under
`C:\Users\56922\.tradingagents\logs\<ticker>\<analysis-date>\runs\<run-id>`, not
the repository's top-level `logs/` directory.

Runs examined:

| Ticker | Analysis date | Run ID | Result | Duration | Log size |
|---|---|---|---|---:|---:|
| `000725.SZ` | 2026-07-15 | `20260714_090803` | Completed | 18m32s | 188,535 bytes |
| `000021.SZ` | 2026-07-14 | `20260714_083927` | Completed | 18m56s | 193,390 bytes |
| `600360.SS` | 2026-07-14 | `20260713_174749` | Completed | 16m21s | 216,291 bytes |
| `512210.SH` | 2026-07-13 | `20260713_170107` | Failed | 17s | See failure below |

## Findings

### Important: Chinese financial units are mistranslated and affect decisions

Confidence: 99%.

The `000725.SZ` news source says `50.00亿元~55.00亿元`, which equals CNY
5.0-5.5 billion:

- Source log:
  `C:\Users\56922\.tradingagents\logs\000725.SZ\2026-07-15\runs\20260714_090803\message_tool.log:29`

The generated news report translates it as `¥50.0-55.0 billion`, a 10x error:

- `D:\prj\TradingAgents\TradingAgents\reports\000725.SZ_20260714_092642\1_analysts\news.md:17`

Downstream agents then treat the source as mathematically inconsistent and
replace it with an unsupported estimate:

- Research manager:
  `D:\prj\TradingAgents\TradingAgents\reports\000725.SZ_20260714_092642\2_research\manager.md:12`
- Portfolio decision:
  `D:\prj\TradingAgents\TradingAgents\reports\000725.SZ_20260714_092642\5_portfolio\decision.md:5`

The `000021.SZ` report confirms the issue is systematic: `200.62亿元` becomes
`¥200.62 billion` while `394.82亿元` is correctly rendered as roughly
`¥39.5 billion` in the same report:

- `D:\prj\TradingAgents\TradingAgents\reports\000021.SZ_20260714_090246\complete_report.md:316`
- Original news text:
  `C:\Users\56922\.tradingagents\logs\000021.SZ\2026-07-14\runs\20260714_083927\message_tool.log:32`

Impact: source normalization changed the research debate and materially influenced
the final Underweight reasoning.

Recommended implementation:

1. Normalize `元`, `万元`, and `亿元` deterministically before LLM consumption.
2. Preserve raw text, normalized numeric value, currency, and unit together.
3. Add a cross-report numeric-consistency gate before research and portfolio
   synthesis.
4. Test explicitly that `50亿元 == 5_000_000_000 CNY` and
   `200.62亿元 == 20_062_000_000 CNY`.

### Important: the verified market snapshot accepts impossible OHLC data

Confidence: 98%.

For `000021.SZ`, the verified snapshot reports Open 59.88, High 53.99, Low
48.88, and Close 52.51. An open above the daily high is impossible. The raw
BaoStock row reports the correct open of 52.12.

- Both values:
  `C:\Users\56922\.tradingagents\logs\000021.SZ\2026-07-14\runs\20260714_083927\message_tool.log:7`
  and line 10
- Generated report discrepancy note:
  `D:\prj\TradingAgents\TradingAgents\reports\000021.SZ_20260714_090246\complete_report.md:218`
- Validator load/render path:
  `D:\prj\TradingAgents\TradingAgents\tradingagents\dataflows\market_data_validator.py:35`
  and lines 84-102

The Yahoo cache itself contains the invalid July 14 open:

- `C:\Users\56922\.tradingagents\cache\000021.SZ-YFin-data-2021-07-14-2026-07-15.csv`

Recommended implementation:

1. Validate `Low <= Open <= High`, `Low <= Close <= High`, nonnegative volume,
   valid dates, and duplicate-date behavior before declaring a row verified.
2. Quarantine invalid provider rows and attempt the next configured source.
3. Record provider, adjustment mode, retrieval time, and effective trading date
   in the deterministic snapshot.
4. Add regression coverage using this exact impossible OHLC row.

### Important: exact market claims mix providers and effective dates

Confidence: 97%.

The `000725.SZ` verified snapshot says the latest trading row is July 13 with a
close of ¥6.83, while the market report also uses a July 14 VWMA of ¥8.00 and a
separate July 14 close of ¥7.02:

- Verified snapshot:
  `C:\Users\56922\.tradingagents\logs\000725.SZ\2026-07-15\runs\20260714_090803\message_tool.log:11`
- Report source-of-truth value:
  `D:\prj\TradingAgents\TradingAgents\reports\000725.SZ_20260714_092642\1_analysts\market.md:4`
- Conflicting July 14 claims:
  `D:\prj\TradingAgents\TradingAgents\reports\000725.SZ_20260714_092642\1_analysts\market.md:30`
  and line 34

The report interprets the mismatch as evidence of distribution rather than as
an unresolved source/date conflict.

Recommended implementation: make one reconciled as-of snapshot authoritative
for all exact price and indicator claims, or block synthesis until conflicting
providers are reconciled. Never derive a trading signal from an unresolved
source mismatch.

### Important: Shanghai ETF symbols can hard-fail a run

Confidence: 97%.

The `512210.SH` run called `get_verified_market_snapshot` and then aborted with
`NoMarketDataError: Yahoo Finance returned no rows`:

- `C:\Users\56922\.tradingagents\logs\512210.SH\2026-07-13\runs\20260713_170107\message_tool.log:12`
- Failure status:
  `C:\Users\56922\.tradingagents\logs\512210.SH\2026-07-13\runs\20260713_170107\run_status.json`

The symbol resolver covers common Shanghai equity prefixes but not `5xx` ETF
prefixes. It also rejects an explicit `.SH` suffix unless the numeric prefix is
already recognized:

- Prefix definitions:
  `D:\prj\TradingAgents\TradingAgents\tradingagents\dataflows\symbol_utils.py:34`
- Exchange inference:
  `D:\prj\TradingAgents\TradingAgents\tradingagents\dataflows\symbol_utils.py:99`
- Snapshot tool propagates the failure directly:
  `D:\prj\TradingAgents\TradingAgents\tradingagents\agents\utils\market_data_validation_tools.py:23`

Recommended implementation:

1. Canonicalize an explicit `.SH` exchange suffix to Yahoo `.SS`.
2. Add Shanghai ETF and index-fund prefix coverage.
3. Add tests for both `512210.SH` and `512210.SS`.
4. Stop retrying equivalent no-data calls and produce a graceful degraded/no-data
   report after providers are exhausted instead of aborting the whole graph.

### Important optimization: PIT ingestion rewrites growing manifests

Confidence: 94%.

The new PIT ingestion code appends every partition record to an ever-growing
tuple and rewrites the complete run manifest after each partition:

- `D:\prj\TradingAgents\TradingAgents\tradingagents\picker\ingestion.py:247`

The global cache manifest is also rewritten on each pending, failed, or complete
transition:

- `D:\prj\TradingAgents\TradingAgents\tradingagents\picker\cache.py:48`
- `D:\prj\TradingAgents\TradingAgents\tradingagents\picker\cache.py:152`

For a multi-year backfill with thousands of daily partitions, cumulative
serialization and write volume grows approximately O(n^2). Resume also hashes
both raw and normalized files for every skipped partition:

- `D:\prj\TradingAgents\TradingAgents\tradingagents\picker\cache.py:112`
- `D:\prj\TradingAgents\TradingAgents\tradingagents\picker\cache.py:189`

Recommended implementation: use an append-only journal or SQLite manifest,
batch checkpoints, and write one final immutable run manifest. Keep full checksum
verification as an explicit integrity audit or cache verified metadata between
ordinary resume operations.

This is an optimization issue, not evidence that current PIT snapshots are
incorrect.

### Optimization: runtime logs are large but phase timing is incomplete

Confidence: 95%.

Recent successful runs take 16-19 minutes, make roughly 23-27 tool calls, and
write 188-216 KB logs. Individual lines can contain entire OHLCV or financial
tables and reach tens of kilobytes. Each examined successful run also contains
five unavailable China-local source entries while still completing in partial or
degraded mode.

Recommended implementation:

1. Store large raw tool payloads separately or content-addressed.
2. Keep `message_tool.log` to bounded previews, row counts, source, date range,
   checksum, and payload reference.
3. Add timings for research debate, trading, risk debate, portfolio synthesis,
   and report writing so the post-analyst portion of the 16-19 minute runtime is
   attributable.
4. Pass deterministic feature summaries between agents instead of repeating
   complete raw tables and debate histories.

## Recent merge assessment

No confirmed correctness defect was found in the new PIT foundation or BaoStock
session-locking implementation during this audit. The merge follows its documented
Phase-1 boundaries. The main code-level concern is manifest/checksum scaling.

The repository's top-level `logs/` directory being empty is not a bug. Default
`results_dir` points to `~/.tradingagents/logs`. The top-level `reports/` folders
are optional copies created after successful completion.

## Verification performed

- Dependency-independent changed-test subset rerun: 61 passed in 2.82 seconds.
- Earlier broader targeted sweep: 153 passed; 32 failed because the local virtual
  environment has neither `pyarrow` nor `fastparquet`.
- The missing Parquet engine is an environment limitation, not a source defect:
  - `pyproject.toml:52-54` declares the optional `pit` extra with
    `pyarrow>=16.1,<26`.
  - `.github/workflows/ci.yml:28` installs `.[dev,pit]`.
- Ruff passed on the changed code and tests.
- `git diff --check fb67d3b..HEAD` passed.
- The worktree was clean before this handoff file was added.
- Do not claim that the full PIT suite passes locally unless the `pit` extra is
  installed and the 32 Parquet-dependent tests are rerun.

## Recommended next work, in priority order

1. Implement deterministic Chinese monetary-unit normalization and cross-report
   numeric consistency checks. This has already altered a final investment thesis.
2. Add OHLC invariant validation and provider/date reconciliation to the verified
   market snapshot.
3. Fix Shanghai ETF symbol canonicalization and graceful no-data handling.
4. Add regression tests based on the exact `000725.SZ`, `000021.SZ`, and
   `512210.SH` evidence above.
5. Profile a representative multi-year PIT backfill, then replace repeated full
   manifest rewrites if measured I/O confirms the expected scaling cost.
6. Reduce log payload duplication and add complete phase timing.
7. Install `.[pit]` in an isolated environment and rerun the complete changed PIT
   test suite before landing any remediation.

## Suggested continuation prompt

Use this in a new conversation:

> Continue the audit remediation documented in
> `docs/2026-07-16-recent-code-reports-logs-audit.md`. Work from the isolated
> worktree/branch recorded there. First verify the evidence and current git state,
> then propose a surgical implementation plan for the first three Important
> findings. Do not mix the PIT manifest performance optimization into the
> correctness fixes. Use tests first and preserve all unrelated user changes.

## Notes and gotchas

- Repository instructions require CodeGraph before structural code exploration.
- Initialize and verify CodeGraph after creating any new worktree.
- `RTK.md` requires shell commands to go through `rtk`; PowerShell commands should
  use `rtk proxy pwsh`.
- The audit worktree uses the same commit as `main`, but this handoff file is an
  uncommitted audit artifact unless later staged or committed explicitly.
- No packages were installed because changing the user's environment was outside
  the read-only audit scope.

# A-share Data Quality Design

Date: 2026-07-01

## Context

Recent analysis of local run artifacts found four related quality problems in the
China A-share workflow:

- The latest complete report for `301526.SZ` finished successfully, but the
  consolidated report is too long because it embeds complete bull, bear, and
  risk debate histories.
- The newer `688519.SS` run created a run directory and an empty reports
  directory, then stopped before completion without an explicit status artifact.
- A-share enrichment sources degrade frequently under the current AkShare
  version and network conditions. Observed failures include `Length mismatch`,
  `KeyError`, endpoint timeouts, and SZSE SSL EOF errors.
- `ak_candidates.csv` contains empty dynamic PE values. The candidate picker
  currently treats that as if valuation filtering is simply unavailable, so
  highly liquid but potentially overvalued names can still rank near the top.

## Goals

1. Prevent candidate selection from silently accepting missing valuation data.
2. Make China A-share enrichment failures explicit, readable, and less likely to
   pollute subsequent runs through cache.
3. Keep the consolidated report focused on decisions and summaries while
   preserving full debate histories in their section files.
4. Leave a durable run-status artifact so incomplete runs can be diagnosed
   without reading the entire message log.
5. Preserve the current vendor routing architecture and avoid a broad data-source
   registry rewrite.

## Non-goals

- Do not change the LLM provider or model selection flow.
- Do not change the trading strategy or final decision semantics.
- Do not introduce paid China market data providers.
- Do not rewrite all AkShare adapters.
- Do not remove existing per-section report files.

## Recommended Approach

Implement the medium-scope "data quality first" path. It touches four narrow
areas: candidate scoring, A-share enrichment, report assembly, and run status.
This gives better reliability and observability without changing the core graph
or vendor abstraction.

Alternatives considered:

- Minimal fix: only patch AkShare endpoint handling and status logs. This leaves
  candidate quality and report readability unresolved.
- Broad registry: introduce a full data-source health registry and provider
  scoring system. This is cleaner long term, but too large for the current
  problem and would expand the testing surface substantially.

## Component Design

### 1. Candidate Quality Gate

File: `ak_pick_a_stock.py`

Add valuation status to the candidate pipeline:

- Detect whether the dynamic PE column exists and has usable non-null numeric values.
- Recognize reasonable alternate PE columns if AkShare returns a different spot
  schema.
- If valuation is available, keep the existing positive and upper-bound PE
  filter.
- If valuation is missing for a row, assign `valuation_data_status=missing`.
- Missing valuation must not be treated as neutral. Either exclude such rows by
  default or apply a large score penalty.

Default recommendation: exclude rows with missing valuation from the top
candidate list unless the whole source lacks usable valuation fields. If the
whole source lacks valuation fields, keep a degraded list but mark every row and
print a warning before writing `ak_candidates.csv`.

The output CSV should include:

- `valuation_data_status`
- `candidate_warning` or equivalent short reason when a row is degraded

### 2. A-share Enrichment Degradation and Cache Policy

File: `tradingagents/dataflows/china_a_enhancements.py`

Introduce an internal status model for enrichment sections:

- `ok`: at least one source returned valid records and no required source failed.
- `partial`: at least one source returned valid records and at least one source
  failed or returned no usable records.
- `failed`: all sources in the requested enrichment category failed or returned
  no usable records.

Cache behavior:

- Cache `ok` snapshots with the existing TTL.
- Cache `partial` snapshots with the existing TTL, but include the degraded
  status and failed source details.
- Do not cache `failed` snapshots, or cache them with a very short TTL such as
  5 minutes.

Schema handling:

- Treat AkShare column drift as a normal degraded condition, not an opaque
  uncaught exception.
- Prefer column discovery and optional fields over fixed assumptions where
  observed endpoints are unstable.
- Include source names, status, and concise error summaries in the rendered
  snapshot.

The prompt-facing text should remain explicit:

> Do not fabricate missing A-share enhancement values. Treat degraded sources as
> unavailable, not as negative or positive evidence.

### 3. Summary-first Report Assembly

File: `tradingagents/reporting.py`

Keep existing per-section files:

- `2_research/bull.md`
- `2_research/bear.md`
- `4_risk/aggressive.md`
- `4_risk/conservative.md`
- `4_risk/neutral.md`

Change `complete_report.md` to prioritize:

1. Portfolio manager decision.
2. Trader investment plan.
3. Research manager decision.
4. Analyst reports.
5. Appendix index that points to full debate files.

Do not embed full bull, bear, aggressive, conservative, or neutral histories in
the consolidated report by default. The detailed files remain the source of
truth for the full debate transcript.

### 4. Run Status Artifact

Likely files: `cli/main.py`, and any shared run-log helper if present.

Each run directory should contain `run_status.json` with:

- `ticker`
- `analysis_date`
- `selected_analysts`
- `started_at`
- `updated_at`
- `completed_at`
- `status`: `running`, `completed`, or `failed`
- `current_phase`
- `error_summary`
- `reports_written`

Status update rules:

- Write `running` when the run directory is created.
- Update `current_phase` when major phases begin: market, sentiment, news,
  fundamentals, research, trading, risk, portfolio, report writing.
- Write `completed` only after reports are written successfully.
- Write `failed` in a top-level exception path with a short error summary.

The existing message log remains unchanged. The JSON status is the quick health
check.

## Data Flow

The core workflow remains unchanged:

1. Candidate selection uses AkShare spot data.
2. A formal run resolves the ticker identity.
3. Market data routes through the configured vendor chain. China A-share config
   currently prefers `akshare,baostock,yfinance`.
4. Analyst prompts receive market data, news, fundamentals, macro data, and
   optional China A-share enrichment.
5. The graph produces final state.
6. Report writers persist per-section reports and the consolidated report.
7. Run status is finalized.

The new design adds quality metadata at steps 1, 4, 6, and 7.

## Error Handling

- Core data failures should remain loud. If no usable market data exists, the
  existing no-data sentinel behavior should continue.
- Optional enrichment failures should degrade, not abort.
- Failed optional enrichment should not be cached for the normal 6-hour TTL.
- Candidate-picker source failure should still exit non-zero.
- Candidate-picker valuation degradation should produce an explicit warning and
  output metadata.
- Report assembly should not fail if a detailed debate section is missing.
  Missing sections should simply be absent from the appendix index.
- Run-status writing should be best effort and should not mask the original
  analysis exception.

## Testing Plan

Add or update focused tests:

- Candidate picker:
  - PE column present and valid.
  - PE column present but row values missing.
  - PE column absent for the whole source.
  - Degraded warning and CSV metadata are written.

- A-share enrichment:
  - All sources succeed -> status `ok`, normal cache.
  - Some sources fail -> status `partial`, cache with degraded details.
  - All sources fail -> status `failed`, no normal TTL cache.
  - AkShare schema drift is rendered as degraded source status.

- Reporting:
  - `complete_report.md` contains portfolio/trader/manager summaries.
  - Full debate histories are written to detailed files.
  - Full debate histories are not embedded in the consolidated report.
  - Appendix entries appear only for files that exist.

- Run status:
  - New run starts with `running`.
  - Successful report write marks `completed`.
  - Simulated exception marks `failed` with `error_summary`.

## Verification

Minimum verification commands:

```bash
rtk pytest tests/test_ak_pick_a_stock.py tests/test_china_a_enhancements.py tests/test_reporting.py
```

If run-status tests land in an existing CLI test file, include that file too.

Manual checks:

- Generate candidates from a mocked or live spot frame and confirm missing PE is
  visible in the output.
- Inspect a generated `complete_report.md` and confirm it is summary-first.
- Inspect a failed or interrupted run directory and confirm `run_status.json`
  explains the last phase.

## Open Decisions

Default candidate behavior for missing valuation should be confirmed during
implementation:

- Recommended: exclude missing valuation rows from the top list when at least
  some rows have valid valuation.
- Fallback: if the entire source lacks valuation, keep degraded rows with a large
  penalty and explicit warnings.

This balances safety with usefulness when AkShare temporarily omits a field.

## Self-review Notes

- No incomplete markers remain.
- The design is scoped to four targeted modules and avoids a provider-registry
  rewrite.
- Core versus optional data failure behavior is explicit.
- Testing criteria cover the observed failures from recent logs and reports.

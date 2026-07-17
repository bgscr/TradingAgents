# Evidence integrity and runtime efficiency remediation

Status: ready-for-agent

## Source

This specification resolves the findings in `docs/2026-07-16-recent-code-reports-logs-audit.md` through the decisions recorded in `docs/adr/0001` through `0004`.

## Objective

TradingAgents must issue a directional decision only when relatively critical evidence is valid and sufficient for the thesis actually used. Missing optional evidence must reduce coverage without unnecessarily preventing a decision. The remediation must also remove measured quadratic PIT-manifest I/O and keep diagnostic logging off the analysis critical path.

## Required behavior

### Evidence acquisition

- Preserve original Chinese source text and extract source-linked monetary facts before LLM consumption.
- Normalize `元`, `万元`, and `亿元`, including ranges, into CNY values without replacing the raw expression.
- Validate OHLCV schema, numeric coercion, `Low <= Open/Close <= High`, nonnegative volume, valid dates, duplicate-date policy, staleness, provider, adjustment basis, retrieval time, and effective trading date.
- Quarantine invalid provider data and continue through the configured provider chain.
- Use one accepted frame for exact prices and all derived indicators in an analysis.
- Resolve explicit mainland exchange suffixes authoritatively and distinguish equities, funds, and indices so capabilities are routed by instrument kind.

### Decision readiness

- Analysts emit source-linked structured material claims alongside human-readable narrative.
- The admission gate requires resolved identity, a valid Authoritative Market Snapshot, known adjustment basis and effective date, and enough history for requested calculations.
- The decision gate validates every material premise actually used by the draft thesis.
- Optional unavailable sources are reported as degraded and do not block by themselves.
- Permit one constrained draft revision that removes unsupported premises or unsupported precision.
- If material evidence remains insufficient or conflicted, complete without a rating, target, entry, stop, or position size.
- Add decision confidence and evidence coverage separately from direction; degradation constrains confidence and permitted claims rather than mechanically changing direction.

### PIT state

- Benchmark a representative ten-year backfill before changing persistence.
- Store mutable cache and run state in SQLite with transactional, indexed updates.
- Emit a final immutable JSON manifest.
- Avoid rehashing every skipped payload during ordinary resume; provide explicit full integrity verification.
- Target manifest bookkeeping below 10% of wall time and approximately constant per-partition update cost.

### Runtime artifacts

- Store complete tool payloads once, compressed and addressed by SHA-256.
- Keep bounded previews and metadata in primary logs.
- Use a bounded per-run background queue and batch writes by size/time.
- Flush at phase boundaries and finalization; synchronously persist only critical state transitions and fatal errors.
- Record graph-phase, tool, model, report, queue, flush, byte, and dropped/coalesced-event metrics.
- Do not scan, reread artifacts, or run garbage collection during active analysis.
- Provide reference-aware offline garbage collection with a dry-run mode.

## Rollout

1. Land source correctness and instrument support.
2. Land structured evidence and decision gates.
3. Replay recent runs in shadow mode and inspect every unexpected block.
4. Enable fail-closed enforcement by default; label any explicit legacy override as unenforced.
5. Land the separately benchmarked PIT migration.
6. Land the separately benchmarked runtime-artifact changes.

## Acceptance scenarios

- `50亿元` equals CNY `5_000_000_000`; `200.62亿元` equals CNY `20_062_000_000`; ranges retain both endpoints and raw text.
- The impossible `000021.SZ` OHLC row is rejected, quarantined, and followed by the next configured provider.
- Exact OHLCV and indicators for `000725.SZ` share provider, effective date, and adjustment basis; an unresolved material contradiction blocks the draft.
- `512210.SH` and `512210.SS` resolve to the same Yahoo symbol and receive fund-appropriate market capabilities without company fundamentals.
- Runs with unavailable optional China-local sources may still decide when their used material premises are supported.
- Runs without trustworthy required price evidence finish with an insufficient-evidence report rather than crashing or returning `Hold`.
- Known bad audit replays would block; valid partial-data replays would pass with explicit degradation.
- PIT and logging benchmarks demonstrate reduced write amplification without weakening recoverability or audit references.

## Non-goals

- Ranking, execution simulation, corporate-action accounting, and walk-forward evaluation remain outside this remediation.
- Providers are not queried merely to vote on already valid data.
- Evidence quality alone does not invent portfolio sizing without portfolio and risk-budget context.
- Automatic artifact retention limits are deferred until storage-growth data exists.

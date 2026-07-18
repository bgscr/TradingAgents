# PIT mutable-state benchmark — 2026-07-17

## Workload

The before benchmark exercises the implementation at commit `ee68af9` plus
Issues 01–05, before the Issue 06 persistence migration:

- 7,500 synthetic daily partitions, representing roughly ten years across the
  `daily`, `daily_basic`, and `suspend_d` datasets.
- Two cache-state transitions per partition (`pending` then `complete`).
- One growing run-manifest write per partition plus the initial run record.
- The real legacy `Manifest` and `IngestionRunManifest` serializers and the
  real `PITCache` state-update paths.
- JSON payloads were fully serialized and counted, but discarded instead of
  being fsynced to persistent storage. The measured time is therefore a lower
  bound on the legacy persistence cost.
- Ordinary-resume verification used 7,500 complete records pointing to two
  256KiB files, preserving the legacy checksum path while bounding temporary
  storage. This caused 3.93GB of actual SHA-256 input.

## Before migration

| Metric | Legacy JSON result |
|---|---:|
| Partitions | 7,500 |
| Cache state transitions | 15,000 |
| Cache state wall time | 307.570s |
| Cache JSON serialization time | 207.753s |
| Cache cumulative JSON bytes | 29,234,080,000 |
| Cache latency, first 100 partitions | 0.369ms/partition |
| Cache latency, middle 100 partitions | 38.286ms/partition |
| Cache latency, last 100 partitions | 85.584ms/partition |
| Cache latency p50 / p95 | 37.865ms / 83.884ms |
| Run-manifest writes | 7,501 |
| Run-manifest wall time | 218.757s |
| Run JSON serialization time | 122.914s |
| Run cumulative JSON bytes | 14,019,479,014 |
| Run latency, first 100 partitions | 0.269ms/partition |
| Run latency, middle 100 partitions | 23.384ms/partition |
| Run latency, last 100 partitions | 40.650ms/partition |
| Run latency p50 / p95 | 22.781ms / 62.343ms |
| Total bookkeeping wall time | **526.328s** |
| Total manifest serialization time | **330.666s** |
| Total cumulative manifest bytes | **43,253,559,014** |
| Ordinary-resume checksum time | 8.955s |
| Ordinary-resume SHA-256 input | 3,932,160,000 bytes |

The first/middle/last latency curve confirms the expected growing-update cost;
the legacy implementation is not approximately constant per partition.

## After migration

The same 7,500-partition workload was rerun against transactional SQLite state.
Unlike the legacy lower-bound benchmark, this run performed real SQLite commits,
checkpointed the WAL, and wrote the final immutable JSON manifest.

| Metric | SQLite result |
|---|---:|
| Partitions | 7,500 |
| Cache state transitions | 15,000 |
| Cache state wall time | 26.065s |
| Cache latency, first 100 partitions | 3.754ms/partition |
| Cache latency, middle 100 partitions | 2.999ms/partition |
| Cache latency, last 100 partitions | 3.018ms/partition |
| Cache latency p50 / p95 | 3.059ms / 6.042ms |
| Run-state appends | 7,500 |
| Run-state wall time | 11.903s |
| Run latency, first 100 partitions | 1.601ms/partition |
| Run latency, middle 100 partitions | 1.572ms/partition |
| Run latency, last 100 partitions | 1.589ms/partition |
| Run latency p50 / p95 | 1.549ms / 1.692ms |
| Final manifest wall time | 0.094s |
| Final immutable JSON bytes | 3,692,764 |
| Checkpointed SQLite database bytes | 5,148,672 |
| Total bookkeeping wall time | **38.061s** |
| Default 40-call/min provider floor | 11,250s |
| Bookkeeping share of rate-limited wall | **0.337%** |
| Ordinary-resume metadata time | 0.324s |
| Ordinary-resume SHA-256 input | **0 bytes** |

The Windows process write-byte counter returned zero for the run and is not
treated as valid evidence. Persisted byte reporting therefore uses the
checkpointed database and final-manifest sizes above; no cumulative-write claim
is made for SQLite beyond those measured artifacts.

## Comparison

- Bookkeeping wall time improved from 526.328s to 38.061s: **13.8× faster**.
- Ordinary resume improved from 8.955s to 0.324s: **27.7× faster**, while moving
  full payload hashing to the explicit integrity audit.
- Cache update latency is approximately constant: 3.754ms initially, 2.999ms
  at the midpoint, and 3.018ms at the end. Run append latency is likewise flat.
- Repeated mutable JSON serialization (43.25GB in the legacy benchmark) is gone.
  The new path emits one 3.69MB immutable run manifest at finalization.
- At the configured default Tushare rate floor, mutable-state bookkeeping is
  0.337% of wall time, satisfying the below-10% target with substantial margin.

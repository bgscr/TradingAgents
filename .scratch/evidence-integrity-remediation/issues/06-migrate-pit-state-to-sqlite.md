# Benchmark and migrate PIT state to SQLite

Status: completed

Measure current manifest and resume costs on a representative ten-year backfill, migrate mutable state transactionally to SQLite, retain final immutable JSON output, and repeat the benchmark.

## Acceptance

- Before/after results report wall time, manifest time, bytes written, update latency by manifest size, and resume hashing cost.
- Per-partition state transitions are transactional and approximately constant-time.
- Interrupted runs resume without losing completed partition provenance.
- Ordinary resume avoids full payload rehashing; an explicit integrity audit retains full verification.
- Finalized-run JSON remains immutable and compatible or has a documented migration path.

## Comments

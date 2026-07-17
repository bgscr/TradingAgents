---
status: accepted
---

# Store mutable PIT ingestion state in SQLite

Mutable partition and run state for point-in-time ingestion will move from repeatedly rewritten JSON manifests to SQLite, with one immutable JSON manifest emitted when a run finalizes. This preserves crash-safe resume and indexed updates while avoiding growing full-manifest rewrites; ordinary resume uses stored verified metadata, while full checksum verification remains an explicit integrity audit.

## Consequences

The migration must be justified by before-and-after backfill benchmarks and must preserve existing content-addressed payloads and finalized-run provenance.

---
status: accepted
---

# Use a dedicated SQLite market-history database

The Point-in-Time Market History Store will use a dedicated SQLite Market History Database for normalized observations, immutable revision identities, Adjustment Factor Revisions, suspension status, provenance, ingestion runs, and snapshot pins. Full provider payloads and other large immutable artifacts remain content-addressed files referenced from SQLite by digest. The database is not shared with the picker subsystem or runtime audit-artifact index.

## Consequences

SQLite transactions, foreign-key enforcement, write-ahead logging, and full synchronous durability provide crash-safe publication while preserving indexed range and as-of queries. Schema versions and migrations must be explicit and transactional. Content-addressed artifacts are installed atomically before their referencing transaction commits, with failed or unreachable candidates cleaned by reference-aware maintenance. The design avoids mutable per-symbol CSV caches and keeps the existing picker lifecycle independently deployable and recoverable.

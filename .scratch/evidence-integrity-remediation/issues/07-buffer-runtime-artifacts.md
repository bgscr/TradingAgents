# Buffer and deduplicate runtime artifacts

Status: completed

Move complete tool payloads to compressed content-addressed artifacts, retain bounded log previews/references, add a bounded background writer, and instrument the entire post-analyst graph and logging path.

## Acceptance

- Identical payloads are stored once and never reread during an active run unless explicitly requested.
- Small events are batched; large payloads are written sequentially and atomically finalized.
- Critical statuses and fatal errors are synchronous; noncritical queue saturation cannot stall indefinitely and is reported.
- Metrics include all graph phases, tool/model/report duration, queue depth, bytes, flush latency, and dropped/coalesced details.
- Reference-aware offline garbage collection supports dry-run and never runs during analysis.

## Comments

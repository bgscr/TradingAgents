---
status: accepted
---

# Buffer and deduplicate runtime artifacts

Complete tool payloads will be stored once as compressed content-addressed artifacts while primary logs retain bounded previews, metadata, and references. A bounded background writer batches noncritical events and flushes at phase boundaries, keeping synchronous durability for status transitions and fatal errors so observability does not stall analysis execution.

## Consequences

The system may lose a small tail of noncritical diagnostics on a hard crash, but it must report coalesced or dropped detail. Garbage collection is explicit, reference-aware, and performed outside active analyses until storage-growth measurements justify an automatic policy.

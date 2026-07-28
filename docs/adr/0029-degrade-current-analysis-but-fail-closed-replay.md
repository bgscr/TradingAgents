---
status: accepted
---

# Degrade current analysis but fail closed historical replay

If the Market History Database is unavailable, corrupt, or unable to commit, a current mainland analysis may enter History Store Degradation and use the existing configured live provider chain. The resulting Authoritative Market Snapshot must be built wholly from one independently validated provider frame, carry a `history_store_degraded` diagnostic, and satisfy every existing evidence and decision gate before a Trading Decision is permitted.

## Consequences

Stored and live rows are never blended to mask a partial or failed history transaction. A failed history-store write cannot invalidate an otherwise complete current live artifact, but it is observable and retried outside the decision contract. Strict historical decision replay fails closed when its pinned revision is unavailable because a present-day provider response cannot recreate observed point-in-time evidence. The independent audit-artifact lifecycle continues to preserve the accepted live artifact when available.

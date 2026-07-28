---
status: accepted
---

# Use BaoStock initially for strict mainland history

BaoStock will be the initial Strict History Provider for mainland Instruments because its single TCP service exposes unadjusted OHLCV, explicit trading status, and dated adjustment factors as one Provider History Bundle. Strict historical reconstruction fails closed when no qualified BaoStock bundle is available.

## Consequences

This does not reorder current mainland acquisition: the Current Analysis Provider Chain remains AKShare, BaoStock, then Yahoo. AKShare's Eastmoney history endpoint and its separate Sina factor function are not combined into one bundle. Yahoo remains eligible as an independently validated current-analysis fallback but is current-only until its raw prices, actions, availability semantics, and derived adjustment lineage pass a separate qualification. Every request remains governed by ADR-0031's shared budgets and priorities.

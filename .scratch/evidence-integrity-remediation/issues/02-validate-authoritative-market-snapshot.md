# Validate and reconcile the authoritative market snapshot

Status: ready-for-agent

Add full OHLCV validation and provenance, route the snapshot through the configured market provider chain, quarantine invalid rows, and derive every exact indicator from the accepted frame. Do not blend providers.

Blocked by: 01

## Acceptance

- The audited impossible `000021.SZ` row fails `Low <= Open <= High` and triggers fallback.
- Duplicate dates, invalid dates, negative volume, stale data, and missing required fields have explicit policies and tests.
- The snapshot records provider, retrieval time, adjustment basis, requested date, and effective trading date.
- The `000725.SZ` regression cannot mix provider/date frames for exact claims.

## Comments

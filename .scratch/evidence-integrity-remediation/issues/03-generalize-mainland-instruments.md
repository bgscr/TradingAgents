# Generalize mainland instrument resolution

Status: completed

Introduce exchange and instrument-kind aware resolution while preserving existing equity outputs. Treat explicit `.SH`, `.SS`, and `.SZ` suffixes as authoritative, support relevant fund prefixes for bare symbols, and route capabilities by instrument kind.

## Acceptance

- Existing representative Shanghai and Shenzhen equity resolutions remain byte-for-byte equivalent.
- `512210.SH` and `512210.SS` canonicalize to `512210.SS` for Yahoo and the corresponding BaoStock/AKShare identifiers.
- Funds receive OHLCV/indicator capabilities but not company-only fundamentals or enhancements.
- `resolve_china_a_symbol` remains a documented compatibility path during migration.

## Comments

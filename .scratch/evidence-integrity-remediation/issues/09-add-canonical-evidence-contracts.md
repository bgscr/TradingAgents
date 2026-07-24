# Add canonical evidence and acquisition contracts

Status: completed

Introduce versioned, closed Pydantic contracts for artifacts, facts, acquisition outcomes, Instrument Identity and Capability Profiles, Calculation Definitions and Lineage, stable identifiers, and JSON/checkpoint compatibility adapters. Preserve raw artifacts separately and categorically exclude provider errors from evidence.

## Acceptance

- Fact IDs are stable across model wording and runtime tool-call IDs.
- Unknown fields, malformed digests/spans, invalid units/dates, and incompatible local fields fail validation.
- Fund identity requires authoritative symbol, venue, kind, currency, and provenance but not display name.
- Rate-limit/timeout/no-data outcomes cannot produce Source Facts.
- Insufficient calculation history cannot emit a fact under the registered derived-field identity.
- Focused deterministic and property-ready tests pass.

## Comments


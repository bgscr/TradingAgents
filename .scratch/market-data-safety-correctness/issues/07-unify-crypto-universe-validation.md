# 07 — Unify Supported Crypto Universe validation

Status: ready-for-agent

Blocked by: None — can start immediately

## Parent specification

[Market-data trading safety and point-in-time correctness](../spec.md). Its cross-cutting invariants, compatibility requirements, acceptance definitions, Supported Crypto Universe, and Out of Scope section are binding.

## Problem being solved

Registry maintenance must not publish a Crypto Instrument that runtime routing cannot analyze. Candidate validation and runtime resolution need one authoritative Supported Crypto Universe policy, with transactional rollback when a custom candidate is unreachable or invalid.

## What to build

Make registry refresh/validation and runtime routing consume the same policy for exactly the Supported Crypto Universe defined in the parent specification.

- Reject unsupported base/quote pairs, quote substitution, duplicate canonical identity, invalid aliases, missing runtime capability, inconsistent `CCC` Reference Market, and any Instrument outside the defined universe before publication.
- Preserve Crypto Symbol Alias semantics: an alias may change spelling but never the identity-defining base asset or quote currency.
- Validate the complete candidate and focused runtime compatibility before replacing any registry, checksum, or configuration.
- On validation, publication, focused-test, or interruption failure, restore the previous registry, checksum, and configuration byte-for-byte.
- Keep runtime handling for an unsupported symbol such as `UNI-USD` typed and non-directional with `registry_not_configured` and zero model calls.
- Make every and only Supported Crypto Universe member pass the shared policy.

## Explicit non-goals

- Do not expand the Supported Crypto Universe, add executable venues, substitute stablecoin quotes, or unify crypto and mainland registries.
- Do not ship/activate the standard registry; Ticket 08 owns packaged registry publication.
- Do not change graph construction, Capability Profiles, Strategy Rules, or calendar semantics; Ticket 09 owns asset-before-graph behavior.
- Do not discover or promote provider metadata at runtime.
- Do not refactor unrelated registry or CLI workflows.

## Blocking dependencies

None. This ticket blocks Ticket 08 and Ticket 12.

## External behavior and audit contract

- Custom refresh rejects `UNI-USD` and any other unsupported identity before publication with a typed validation result.
- Rejection leaves the prior registry, checksum, and configuration unchanged.
- Runtime analysis for an unsupported symbol remains a non-directional `registry_not_configured` outcome with zero model calls.
- Accepted identities preserve their canonical base/quote pair and `CCC` Reference Market; aliases cannot substitute quote currency.
- Validation output identifies the rejected invariant without treating provider metadata or error text as a Source Fact.

## Acceptance evidence

- Before/after hashes for registry, checksum, and configuration proving rejected or interrupted publication is transactional.
- Validation output for `UNI-USD`, quote substitution, invalid alias, duplicate identity, and missing-capability cases.
- Runtime/CLI result showing `registry_not_configured`, non-directional output, and zero model calls for the unsupported control.

## Regression tests

- [ ] Reject an unsupported base/quote candidate before publication.
- [ ] Reject quote-substitution aliases, invalid aliases, duplicate identities, non-`CCC` reference context, and missing runtime capability.
- [ ] Prove every and only parent-spec Supported Crypto Universe member passes the shared policy.
- [ ] Inject validation, focused-verification, publication, and interruption failures; compare registry/checksum/configuration byte-for-byte with the prior state.
- [ ] Run unsupported `UNI-USD` through runtime/CLI and assert typed non-directional output with zero model calls.
- [ ] Prove mainland registry, provider routing, and identity resolution are unchanged.

## Compatibility or migration requirements

- Preserve explicit custom-override precedence, but require the candidate to pass the same shared-universe policy.
- Never rewrite an invalid custom registry/checksum on load; return a typed configuration outcome and require transactional republishing.
- Preserve existing mainland registry/configuration and prior valid crypto artifacts on all failures.

## Acceptance criteria from the specification

- AC8 — transactional custom-candidate rejection and unsupported CLI behavior.
- AC13 — focused/full-suite, lint, no universe expansion, and no-unrelated-refactoring requirements.

## Verification commands

```powershell
pytest -q tests/test_crypto_identity_registry.py tests/test_crypto_identity_registry_refresh.py tests/test_cli_symbol_handling.py tests/test_env_overrides.py
pytest -q
ruff check .
git diff --check
```

## Definition of done

- [ ] Refresh validation and runtime routing share one Supported Crypto Universe policy.
- [ ] Invalid/unreachable custom candidates cannot mutate registry, checksum, or configuration.
- [ ] Unsupported runtime symbols fail typed and non-directionally before model work.
- [ ] The universe and mainland behavior remain unchanged.
- [ ] Focused tests, the full supported suite, Ruff, and diff validation pass.

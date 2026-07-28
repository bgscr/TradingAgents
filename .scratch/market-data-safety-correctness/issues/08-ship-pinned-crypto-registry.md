# 08 — Ship and pin the standard Crypto Instrument Registry

Status: ready-for-agent

Blocked by: 07

## Parent specification

[Market-data trading safety and point-in-time correctness](../spec.md). Its cross-cutting invariants, compatibility requirements, acceptance definitions, Supported Crypto Universe, and Out of Scope section are binding.

## Problem being solved

A supported Crypto Instrument such as `SOL-USD` must resolve in a standard installation without manual environment configuration or dependence on the caller's working directory. Registry bytes, checksum, registry identity, and default configuration must form one coherent deterministic pin.

## What to build

Ship the standard Crypto Instrument Registry and checksum for exactly the parent-spec Supported Crypto Universe, and configure runtime resolution to use their pinned revision by default.

- Package the registry, checksum, registry identifier, and expected digest as standard runtime configuration.
- Resolve default paths relative to the installation/repository configuration rather than the process working directory.
- Treat path and digest as one coherent pin. Missing file, missing digest, partial override, digest mismatch, or registry-ID mismatch fails typed before model work.
- Resolve `SOL-USD` and every other supported member to a canonical Crypto Instrument Identity with the `CCC` Reference Market.
- Preserve explicit custom override precedence only when a coherent path/digest pair passes Ticket 07 validation.
- Keep unsupported symbols typed as `registry_not_configured`; standard registry activation must not widen the universe.

## Explicit non-goals

- Do not expand the Supported Crypto Universe or infer support from Yahoo/provider compatibility.
- Do not resolve the run's Capability Profile or construct crypto graph/checkpoint state; Ticket 09 owns that end-to-end behavior.
- Do not add executable venues, stablecoin substitutions, or a universal registry.
- Do not rewrite invalid custom artifacts on load.
- Do not refactor unrelated package/configuration loading.

## Blocking dependencies

Blocked by Ticket 07 so the shipped registry is validated by the same policy runtime uses. This ticket blocks Ticket 09 and Ticket 12.

## External behavior and audit contract

- Standard configuration resolves `SOL-USD` from any working directory without `registry_not_configured`.
- Resolver evidence identifies the pinned registry ID/digest, canonical identity, instrument kind `crypto`, and `CCC` Reference Market.
- Missing, partial, mismatched, or tampered pin configuration produces a typed pre-model configuration outcome.
- Unsupported symbols remain unsupported even if an external provider recognizes them.

## Acceptance evidence

- Package/configuration inspection showing the shipped registry and checksum agree with the configured registry ID/digest.
- Resolver output for `SOL-USD` and all supported members from at least two working directories.
- Typed failure outputs for missing/partial/mismatched pins and an unsupported-symbol control.

## Regression tests

- [ ] Load/package the standard registry and verify its checksum and exact Supported Crypto Universe.
- [ ] Resolve `SOL-USD` and every supported symbol from multiple working directories.
- [ ] Reject missing file/digest, partial override, digest mismatch, registry-ID mismatch, and tampered bytes before model work.
- [ ] Prove explicit coherent custom overrides retain precedence and still pass Ticket 07 validation.
- [ ] Prove `UNI-USD` remains unsupported and mainland registry behavior is unchanged.
- [ ] Verify packaging/install configuration includes both registry and checksum.

## Compatibility or migration requirements

- Standard installations gain the shipped pin without requiring environment edits.
- Preserve coherent custom override precedence; invalid legacy overrides fail typed and remain unmodified.
- Do not rewrite checkpoints or historical reports. Ticket 09 handles checkpoint compatibility when asset configuration becomes part of the run signature.

## Acceptance criteria from the specification

- AC7 — standard pinned-registry resolution portion.
- AC8 — unsupported symbol remains outside the universe.
- AC11 — existing configuration/report immutability portions.
- AC13 — focused/full-suite, lint, no universe expansion, and no-unrelated-refactoring requirements.

## Verification commands

```powershell
pytest -q tests/test_crypto_identity_registry.py tests/test_crypto_identity_registry_refresh.py tests/test_env_overrides.py tests/test_cli_symbol_handling.py
pytest -q
ruff check .
git diff --check
```

## Definition of done

- [ ] The standard registry/checksum are shipped, pinned, and working-directory independent.
- [ ] `SOL-USD` and every supported member resolve to the expected canonical `CCC` identity.
- [ ] Invalid or partial pins fail typed before model work without modifying custom state.
- [ ] Unsupported and mainland identity behavior remains unchanged.
- [ ] Focused tests, the full supported suite, Ruff, and diff validation pass.

# ISO-02 — Hash-locked offline runtime/dependency bundle and manifest

**Parent specification:** [Mainland qualified routing production composition factory](../spec.md)

**What to build:** Produce a reviewed, content-addressed, secret-free Windows
CPython/runtime and dependency bundle that the network-disabled Sandbox can
verify and install entirely to guest-local paths before later acceptance work.

**Blocked by:** ISO-01 — Versioned Windows Sandbox runner/config and exact
disposable workspace.

**Status:** ready-for-agent

## Observable behavior

- A reviewed supported Windows CPython runtime and the exact offline wheelhouse
  cover the project plus every Ticket 06 development/test dependency.
- A hash-locked dependency manifest resolves every required package without an
  index, network request, host venv, or undeclared artifact.
- Every installer, wheel, executable, DLL, archive, lock/manifest, bootstrap
  script, and support file is SHA-256-bound. Portable Git is included only when
  an actual in-Sandbox acceptance command demonstrably requires it.
- Before execution, the guest independently verifies the complete bundle
  manifest and cross-checks source, bundle, runner/config, and protected-manifest
  identities.
- Dependencies install to guest-local paths with `--no-index`,
  `--require-hashes`, and `--find-links`. The staged project installs with
  `--no-deps --no-build-isolation`.
- Tampered, missing, extra, unhashed, platform-incompatible, secret-bearing, or
  source-mismatched input fails before project import or execution.
- ISO-02 proves bundle construction and offline install only. It does not run
  Ticket 06, Ticket 07, or the adversarial boundary qualification.

## Exact OS containment requirements

- Supply the bundle through ISO-01's reviewed read-only runtime-bundle mapping;
  no bundle component or install target may be writable on the host.
- Install CPython, the environment, dependencies, caches, and tools only beneath
  sandbox-local writable roots with networking disabled.
- Do not invoke the host Python, pip, Git, venv, package cache, credential store,
  temp directory, or user profile from the guest.
- The sole writable host mapping remains the fresh bounded results-export
  folder; bundle construction cannot add another writable mapping.
- Offline install/verification must remain valid with all host integrations and
  credentials absent.

## Input and output identities

Inputs bind the exact reviewed source archive/tree identity from ISO-01, the
supported Windows CPython version and distribution identity, dependency lock,
wheelhouse contents, installer/bootstrap support files, optional portable Git
decision, runner/config identity, protected-manifest identity, and SHA-256 for
every byte-bearing input.

The output is one content-addressed execution-bundle identity plus a canonical
manifest listing relative paths, lengths, SHA-256 values, artifact roles,
platform/runtime compatibility, dependency hashes, source identity, and
runner/config/protected-manifest cross-bindings. Offline verification and install
summaries are bounded safe outputs; caches, installed guest runtime trees,
credentials, and runtime provider databases are not exported.

## Security rules

- Validate that the bundle, lock, wheel metadata, archive members, bootstrap
  arguments, and URLs contain no `.env`, `.env.enterprise`, credentials, cookies,
  index tokens, credentialed URLs, host absolute paths, or production-state
  material.
- Reject symbolic/reparse escapes, duplicate/case-colliding archive members,
  path traversal, unexpected executables/DLLs, unhashed files, and manifest/file
  disagreement.
- Do not contact package indexes, providers, models, update services, or Git
  remotes. All `RUN_LIVE_*` controls remain `0` and
  `PYTHON_DOTENV_DISABLED=1` remains enforced.
- The source identity and bundle identity are mutually bound; neither may be
  swapped independently after review.

## Historical compatibility

- Bundle creation and validation do not open, copy, repair, or rewrite host
  provider ledgers, calendar state, checkpoints, reports/logs, baselines,
  incidents, or Ticket 07 evidence.
- Existing host environments and caches remain untouched and are not evidence
  for offline reproducibility.
- No historical runtime or dependency artifact is relabeled as the new bundle;
  the bundle receives a new additive identity.

## Deterministic regressions

- Rebuild/serialize the manifest deterministically and prove identical inputs
  yield an identical bundle identity.
- Tamper each artifact class and prove missing/extra/changed length or SHA-256,
  source mismatch, unsupported runtime, wrong wheel platform, and incomplete
  dependency closure fail before install.
- Prove network-disabled installation succeeds from only the wheelhouse and
  fails if pip attempts index resolution or dependency/build isolation.
- Prove project installation adds no dependency resolution and the installed
  environment imports the supported deterministic test/tooling surface without
  provider/model I/O.
- Prove secret, credentialed URL, host-path, traversal, reparse, duplicate, and
  unbounded-output canaries fail closed.

## Explicit non-goals

- Running any Ticket 06/QCF/PE/AC command, Ticket 07, live provider, paid model,
  calendar refresh, or baseline freeze.
- Reusing the host venv/package cache, performing a network install, or adding
  portable Git without a proven acceptance requirement.
- Changing project dependency policy beyond the exact supported Ticket 06
  runtime/test closure.
- Mapping protected host state or copying the QCF17 prototype implementation
  into production.
- Enabling or recommending `qualified_v1`.

## Verification commands

Run deterministic manifest/bundle tests and the guest's offline verification
and install smoke only:

```powershell
pytest -q -k "iso_02 or offline_runtime_bundle or hash_locked_wheelhouse or bundle_manifest"
python -m pip install --no-index --require-hashes --find-links <verified-wheelhouse> -r <hash-locked-manifest>
python -m pip install --no-index --no-deps --no-build-isolation <verified-staged-project>
ruff check tradingagents cli tests
git diff --check
```

The two pip commands must run only with the verified supported guest-local
CPython inside the network-disabled disposable guest, never with host Python.
Record the exact resolved paths and identities in safe path-redacted evidence.

## Definition of done

- [ ] The supported CPython runtime, exact wheelhouse, development/test closure,
  optional-Git decision, and every support artifact are fully SHA-256-bound.
- [ ] The guest verifies all source/bundle/runner/config/protected-manifest
  cross-bindings before project import and installs wholly offline with the
  required pip flags to guest-local paths.
- [ ] Tamper, completeness, platform, secret, path, and no-network regressions
  pass with bounded diagnostics.
- [ ] No host venv/cache/state was reused or changed and no Ticket 06/07,
  provider/model, baseline, or activation work occurred.
- [ ] The reviewed bundle identity is ready for ISO-03 qualification.

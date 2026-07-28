# Market-data trading safety and point-in-time correctness

Status: ready-for-agent

## Problem Statement

TradingAgents operators need to trust that a CLI run cannot issue a directional Trading Decision when an authoritative current suspension says the Instrument cannot trade, and that every decision can be traced to the exact point-in-time market-history revisions that produced it. The same trust boundary must survive incremental ingestion, concurrent payload maintenance, Crypto Instrument setup, provider retries, and ordinary local execution.

The eight confirmed findings show that these guarantees are not yet consistent at several integration seams. The remediation must put trading safety first, preserve point-in-time provenance and exact revision membership second, and then restore predictable configuration, request accounting, and worktree hygiene. It must do so without changing Strategy Rules, widening the Supported Crypto Universe, reordering the Current Analysis Provider Chain, or refactoring unrelated code.

## Solution

Make a minimal, seam-focused remediation across seven work areas:

1. Preserve authoritative BaoStock suspension evidence through the accepted live snapshot and stop a confirmed current suspension at the deterministic preflight/Decision Gate.
2. Version Authoritative Market Snapshot identity so it commits to exact observation, trading-status, factor, and calendar membership, and make pin publication exact and collision-safe.
3. Compute incremental-refresh provenance from the revisions in the merged result rather than from whether a bounded refresh repeats every retained date.
4. Implement the prototype-selected cross-process payload-mutation mutex so payload publication and garbage collection cannot leave a durable reference without its immutable payload.
5. Ship and pin the Crypto Instrument Registry, validate custom candidates against the Supported Crypto Universe, and resolve the CLI asset configuration before graph, evidence, and checkpoint construction.
6. Give the Provider Request Coordinator sole ownership of Yahoo retry budgets so every physical attempt is paced, persisted, and auditable under ADR-0031.
7. Ignore only the default generated market-history runtime tree so local executions do not dirty a clean worktree.

The implementation must preserve ADR-0023 exactly: an authoritatively confirmed current mainland suspension yields a non-directional Analysis Outcome with reason `instrument_currently_suspended`, reports the latest genuinely traded close, and performs no directional-memory update. It must also preserve ADR-0031 exactly: every physical provider attempt, including retries, is coordinated by Upstream Service Identity; fallback remains sequential; cooldowns and physical-attempt counts remain shared and persisted; and no fan-out, identity rotation, or throttling probe is introduced.

## User Stories

1. As a mainland investor, I want a confirmed current suspension to prevent Buy, Hold, and Sell output, so that the system never recommends an unavailable trade.
2. As a mainland investor, I want a suspended outcome to show the latest genuinely traded close, so that a carried suspension close is not mistaken for an executable trade.
3. As a risk owner, I want authoritative suspension status to survive live acquisition, History Store Degradation, canonical evidence, and audit rendering, so that no boundary silently weakens ADR-0023.
4. As a risk owner, I want zero-volume or blank provider rows to remain insufficient to infer suspension by themselves, so that malformed data cannot become a trading-status fact.
5. As an operator, I want deterministic safety blockers to stop downstream model work, signal publication, and directional-memory writes, so that a prohibited decision cannot leak through another output channel.
6. As an audit reader, I want a snapshot identity to change when its trading-status revision changes even if OHLCV is identical, so that materially different evidence never shares an identity.
7. As an audit reader, I want each pin to commit to the exact ordered observation and status revisions plus the exact factor and calendar revisions, so that strict replay cannot substitute current data.
8. As a store operator, I want repeated publication of an identical pin to be idempotent and publication of a conflicting membership under the same identity to fail closed, so that pin tables cannot accumulate mixed membership.
9. As a replay user, I want missing or inconsistent pin membership to produce a typed unavailable result, so that replay never repairs evidence from the latest store state.
10. As a point-in-time researcher, I want a bounded incremental refresh to retain Observed Point-in-Time History status when both the retained and refreshed revisions qualify, so that omission of older dates from the request is not misclassified as backfill.
11. As a point-in-time researcher, I want genuinely unseen historical observations to remain Retrospective Backfill, so that a corrected refresh does not weaken the historical replay trust boundary.
12. As a replay user, I want retrieval cutoffs to exclude revisions first observed after the requested as-of time, so that later corrections cannot leak into earlier decisions.
13. As a maintenance operator, I want payload garbage collection to coexist safely with publication from another process, so that maintenance cannot delete bytes that a committed database row references.
14. As a maintenance operator, I want a failed publication to leave at most a recoverable, collectible orphan, so that crash recovery remains simple and reference-aware.
15. As an implementer, I want payload GC to use the protocol proven by the deterministic throwaway prototype, so that production behavior follows verified SQLite and filesystem concurrency guarantees.
16. As a Crypto Instrument user, I want the shipped registry to resolve `SOL-USD` without manual configuration, so that a supported Instrument works in a standard installation.
17. As a Crypto Instrument user, I want the CLI to construct a crypto Capability Profile before analysis begins, so that equity-only semantics never enter the graph or checkpoint.
18. As a Crypto Instrument user, I want the 20-day return rule to use 21 consecutive daily closes including weekends, so that the decision audit reports `20 calendar days` rather than `20 trading sessions`.
19. As a Crypto Instrument user, I want the audit to identify the `CCC` Reference Market and pinned registry revision, so that the recommendation remains venue-agnostic and reproducible.
20. As a registry maintainer, I want refresh validation and runtime routing to share the same Supported Crypto Universe, so that a candidate cannot publish an Instrument that runtime analysis will reject.
21. As a registry maintainer, I want a rejected custom candidate to leave the prior registry, checksum, and configuration intact, so that failed maintenance is transactional.
22. As a mainland user, I want crypto configuration changes to leave mainland identity, provider routing, forward adjustment, and 20-trading-session semantics unchanged, so that secondary-market support cannot regress the Primary Analysis Market.
23. As an upstream-capacity operator, I want every actual Yahoo network attempt counted and paced by the Provider Request Coordinator, so that configured local safety policy reflects physical traffic.
24. As an upstream-capacity operator, I want a retry budget to cap total physical attempts without nested adapter retries, so that a logical request cannot amplify traffic invisibly.
25. As an upstream-capacity operator, I want Yahoo throttling and valid `Retry-After` values to update the shared cooldown while other failures retain their distinct types, so that rate limits do not become Source Facts or generic errors.
26. As a concurrent caller, I want identical Yahoo requests to share one coordinated attempt sequence, so that single-flight behavior remains effective during retries.
27. As a developer, I want default market-history databases, WAL/SHM sidecars, payloads, backups, and temporary maintenance files ignored, so that a normal run leaves a clean worktree clean.
28. As a developer, I want source fixtures, configuration, reports, and unrelated data directories to remain visible to Git, so that the ignore rule does not conceal intentional project changes.
29. As an operator upgrading an existing installation, I want legacy snapshot IDs, pins, databases, payload files, and reports preserved without destructive rewriting, so that historical audit material remains available.
30. As an operator upgrading an existing installation, I want inconsistent legacy state to fail closed or degrade with a typed diagnostic rather than be silently repaired, so that migration cannot invent point-in-time truth.
31. As a CLI user, I want programmatic and CLI entry points to produce the same terminal contract and decision-audit fields for the same deterministic evidence, so that safety does not depend on the invocation path.
32. As a maintainer, I want focused regressions plus the existing full suite and lint checks to pass without unrelated refactoring, so that the remediation is small enough to review and roll back.

## Implementation Decisions

### Finding map and priority

The confirmed findings are referenced without reopening diagnosis:

| Finding | Work area | Priority |
| --- | --- | --- |
| F1 | BaoStock suspension propagation and fail-closed decisions | Critical trading safety |
| F2 | Shipped crypto registry plus CLI asset configuration | Important decision correctness |
| F3 | OPIT incremental-refresh provenance | Important point-in-time correctness |
| F4 | Snapshot identity and exact pin membership | Important point-in-time correctness |
| F5 | Payload garbage-collection concurrency | Important durability correctness |
| F6 | Yahoo retry-budget ownership | Important upstream safety |
| F7 | Custom crypto-registry universe validation | Minor configuration consistency |
| F8 | Runtime market-history ignore rules | Minor developer hygiene |

Trading safety outranks availability. Point-in-time correctness outranks preserving an unsafe legacy interpretation. When the system cannot prove a material status, identity, membership, provenance, or payload invariant that this specification requires, it must return a typed non-directional or unavailable result rather than synthesize, forward-fill, relabel, or substitute evidence.

### Cross-cutting invariants

- Preserve the single-provider Authoritative Market Snapshot and complete Provider History Bundle boundaries. No work item may blend providers or add a request after a complete current candidate solely for future replay.
- Preserve the Current Analysis Provider Chain and its sequential fallback behavior.
- Keep deterministic configuration and evidence blockers before model-mediated analysis whenever the required information is available at preflight.
- New durable identities and audit fields must be deterministic, versioned, and independent of process IDs, tool-call IDs, row insertion order, or model prose.
- Publication is successful only when all durable metadata and every immutable payload it references can be verified together.
- No migration may relabel, re-key, delete, or repair historical evidence in place when the original exact state cannot be proven.
- Make the smallest coherent changes at existing boundaries. Do not create a new general storage layer, retry framework, asset taxonomy, or CLI architecture.

### BaoStock suspension propagation and fail-closed decisions

- The accepted BaoStock candidate must expose its authoritative latest-session trading status and the latest genuinely traded close as part of the same provider-specific evidence bundle as its OHLCV data.
- A confirmed status must survive validation, provider selection, live snapshot construction, History Store Degradation, canonical evidence construction, audit serialization, preflight, and deterministic report rendering without becoming `unknown`.
- Current Tradeability is derived from the latest applicable mainland Market Session represented by authoritative status evidence. `suspended` is used only for an explicit provider- or exchange-confirmed Suspension Observation; a blank or zero-volume row alone remains malformed or unknown according to existing validation policy.
- For a valid current Suspension Observation, the official carried close and zero volume remain in the equity Observation Horizon under ADR-0022 and ADR-0019, but `latest_traded_close` is the most recent earlier close from a session explicitly marked traded, expressed under the snapshot's Adjustment Basis. If no genuinely traded close exists in retained evidence, the field remains unavailable and the report says so without inventing a value.
- Once the selected complete provider candidate authoritatively confirms a current suspension, acquisition must not continue to another provider to seek a directional result. The candidate is valid historical evidence, and the graph terminates non-directionally under ADR-0023.
- A confirmed current suspension must yield `terminal_outcome_kind=analysis_outcome` and reason `instrument_currently_suspended`; publish no Buy, Hold, or Sell decision, signal, position fields, or directional-memory update.
- The deterministic blocker must run before analyst/model stages. Provider acquisition needed to establish status is not counted as downstream model work.
- An internal contradiction or loss between authoritative status evidence and the constructed snapshot is a fail-closed validation error. This specification does not redefine a provider that supplies no authoritative status as suspended, and it does not authorize an extra provider request merely to replace `unknown`.

### Snapshot identity and exact pin membership

- New snapshots use a versioned `snapshot:v2:<sha256>` identity. The digest commits to a canonical membership manifest rather than OHLCV alone.
- For a stored/reconstructed snapshot, the manifest includes canonical Instrument Identity and identity revision, provider dataset/Upstream Service Identity, requested and effective dates, Adjustment Basis, normalized frame digest and row count, derivation/normalization versions, the ordered session-date/Raw Market Observation/trading-status revision tuples, the canonical factor-revision set, the applicable calendar revision, provenance class, and any other material input used by validation or derivation.
- For a current-only live snapshot that has no durable revision IDs, the manifest includes the accepted provider artifact identity and the authoritative trading-status evidence identity and value. A provider with no authoritative status contributes an explicit `unknown` marker. A later durable reconstruction receives its own membership-based identity.
- Two snapshots with identical OHLCV but different trading-status revision identities, Current Tradeability, calendar membership, or other material revision membership must have different v2 IDs.
- Canonical ordering makes the same exact membership produce the same ID regardless of database insertion or input collection order.
- Snapshot pin publication is one atomic operation. The parent metadata, ordered observation/status pairs, factor set, and calendar membership are either all committed or all absent.
- Re-publishing an existing ID is idempotent only when all parent fields and every child membership match exactly. Any mismatch raises a typed identity-collision/corruption outcome and leaves the existing pin and all child rows unchanged; ignore-on-conflict behavior must not merge or partially append membership.
- Strict replay reconstructs from the exact pin. Missing parent metadata, revision rows, membership rows, or referenced payload bytes produces a typed strict-replay-unavailable/corruption result. It never substitutes a newer revision or current provider response.
- Snapshot identity changes must not weaken the immutable Source Artifact digest, single-provider rule, Calculation Lineage, or Decision Assertion validation already in place.

### OPIT incremental-refresh provenance

- An incremental refresh is a bounded delta over the retained bundle. Its omission of older retained dates is not evidence that those dates are Retrospective Backfill.
- Merge the candidate revisions with the exact prior bundle membership first, then derive the resulting provenance from the actual retained and selected revisions and their authoritative availability/first-observed timestamps.
- When all revisions contributing to the resulting bundle/snapshot qualify as Observed Point-in-Time History for the requested as-of cutoff, the aggregate remains `observed_point_in_time`, even if the refresh payload contains only the Revision Refresh Window and missing dates.
- If any contributing revision is Retrospective Backfill, the aggregate cannot be upgraded to Observed Point-in-Time History. A newly retrieved, previously unseen historical effective date remains Retrospective Backfill unless authoritative availability evidence proves otherwise.
- A correction first observed at a later time is eligible only for an as-of cutoff at or after that observation time. Earlier replay continues to select the earlier exact revision or fails closed.
- Provenance classification is independent of request shape, row-count equality, set inclusion between prior and candidate dates, or the order in which revisions were ingested.
- Existing incremental retention, 21-session overlap, monthly reconciliation, and no-overwrite revision behavior remain unchanged.

### Payload garbage-collection concurrency — design gate resolved

The deterministic throwaway prototype selects one global cross-process payload-mutation mutex implemented by `BEGIN IMMEDIATE` on a small SQLite sidecar lock database. The sidecar path is derived deterministically from the canonical Market History Database path, so every process using the same store resolves the same mutex. SQLite releases the mutex automatically when its connection closes or its process terminates. The authoritative evidence is recorded in [REPORT.md](payload-gc-prototype/REPORT.md), [RESULTS.md](payload-gc-prototype/RESULTS.md), [SPEC-AMENDMENT.md](payload-gc-prototype/SPEC-AMENDMENT.md), and [run_prototype.py](payload-gc-prototype/run_prototype.py).

Every operation that reads in preparation to install, installs, verifies, registers, references, deregisters, sweeps, or unlinks a canonical payload must acquire the same mutex before mutating payload files or payload metadata. Lock order is always the sidecar payload-mutation mutex first and a main Market History Database transaction second; reverse acquisition is forbidden. A thread-only mutex is insufficient.

Publication has this required ordering and holds one uninterrupted sidecar-mutex scope through the final verification:

```text
acquire sidecar mutex
→ install or verify canonical payload file
→ register or exactly validate payload row
→ commit every durable reference row
→ verify the committed row and canonical file by digest and byte length
→ release sidecar mutex
```

Existing separate payload-registration and reference-publication transactions may remain only when the same outer mutex spans both. Publication must not release the mutex between payload registration and the final durable reference commit. A committed payload row whose canonical file is missing, has the wrong digest, or has the wrong byte length is corruption and fails closed; publication and readers must not silently reinstall or repair it.

GC's initial candidate scan is advisory. Each candidate uses a separate short mutex scope with this required ordering:

```text
advisory candidate scan
→ acquire sidecar mutex
→ BEGIN IMMEDIATE on the main Market History Database
→ recheck the payload row and every durable reference
→ delete a proven-orphan payload row
→ commit the main-database deletion
→ unlink the canonical payload file
→ release sidecar mutex
```

A referenced or already-missing metadata candidate is skipped without file mutation. The physical unregistered-file sweep treats each path only as a candidate and uses the same global mutex plus a main-database row/reference recheck before unlinking; it must never delete from a stale snapshot of registered digests. GC reports success only when unlink succeeds or the canonical path is already absent while the mutex is held.

The permitted failure residues are explicit:

- Publisher interruption before the final reference commit leaves at most an unregistered or unreferenced verified file/row that a later locked sweep can collect or a later publisher can reuse.
- GC interruption after row-deletion commit but before unlink leaves `row absent, file present`; this is safe and recoverable by a later locked sweep or idempotent publisher.
- GC interruption after unlink leaves `row absent, file absent`.
- Unlink failure leaves `row absent, file present` and is a typed retryable maintenance outcome, not successful collection.

A bounded sidecar-mutex timeout is a typed failure and performs no mutation: publication returns the existing typed history-store write failure/degradation contract, while GC leaves state unchanged for a later maintenance attempt. Different digests remain conservatively serialized for this fix. Per-digest locking is out of scope unless new deterministic evidence justifies its added coordination complexity.

The prototype evaluated 45 protocol/interleaving cases and two separate-process probes. The selected mutex had zero immediate and zero eventual invariant failures. A fully fenced tombstone design was also safe but rejected because it requires main-schema state, owner fencing, quarantine phases, and recovery machinery. Delete/commit/unlink/recheck failed two republication windows; a generation check retained a final time-of-check/time-of-use race; and holding the main database transaction through unlink could roll back a live row after the file was removed.

The selected protocol assumes local SQLite files on a filesystem that honors SQLite cross-process locks; all processes derive the same canonical sidecar path; one Market History Database exclusively owns its payload root; main-database publication and GC retain foreign keys, WAL, `synchronous=FULL`, bounded busy timeouts, and `BEGIN IMMEDIATE`; and canonical installation retains same-directory atomic replacement, durability flushing, and digest/length verification. Network filesystems with unreliable SQLite locking and multiple databases sharing one payload root are unsupported.

The selected protocol must also enforce these protocol-independent invariants:

- After any successful publication or collection, every committed market-history row that references a payload digest has a canonical file whose digest and byte length verify.
- A collector may report a digest as collected only if it cannot be concurrently committed or re-registered as referenced while the collector can still remove its canonical file.
- Publication either commits all referencing rows with the verified payload available, or fails without durable references and leaves at most a later-collectible orphan.
- The unregistered-file sweep cannot delete a file between its atomic installation and its durable registration/reference by a concurrent publisher.
- Concurrent publication of identical bytes remains content-addressed and idempotent.
- A crash after database orphan removal but before file removal may leave an unreferenced file for a later pass; it may not leave a committed reference to missing bytes.
- Readers and strict replay never treat a missing referenced payload as a cache miss. They report corruption/unavailability and follow ADR-0029's current-analysis degradation versus strict-replay fail-closed behavior.
- Garbage collection remains offline/maintenance work and does not enter the active analysis critical path.

### Crypto registry and CLI asset configuration

- The shipped Crypto Instrument Registry, checksum, registry identifier, and expected digest are part of standard runtime/package configuration. Paths resolve independently of the caller's current working directory.
- Registry path and digest are a coherent pin. A missing file, missing digest, digest mismatch, registry-ID mismatch, or partial override produces a typed configuration outcome before model work; runtime never discovers or promotes provider metadata.
- Resolve authoritative Instrument Identity and Capability Profile before graph construction, evidence/checkpoint schema selection, or streaming. The resulting asset configuration is immutable for the run and shared by CLI and programmatic entry points.
- A supported Crypto Instrument uses instrument kind `crypto`, the `CCC` Reference Market, the crypto Capability Profile, and the registered 20-calendar-day return rule over 21 consecutive daily closes including weekends. It must never inherit the equity 20-trading-session calendar because a stock-mode graph was constructed first.
- The asset configuration, registry revision/digest, Reference Market, observation-calendar kind, and rule horizon are included in the checkpoint/configuration signature and decision audit. Resuming a checkpoint under a different asset configuration fails closed or uses an explicit schema migration; it never silently reinterprets equity state as crypto state.
- Registry maintenance and runtime routing use one shared Supported Crypto Universe policy: `BTC-USD`, `ETH-USD`, `SOL-USD`, `XRP-USD`, `ADA-USD`, `DOGE-USD`, `LTC-USD`, `BCH-USD`, `DOT-USD`, `AVAX-USD`, and `LINK-USD`. This work does not expand that universe.
- Custom registry candidates with an unsupported base/quote pair, quote substitution, duplicate canonical identity, invalid alias, missing runtime capability, or inconsistent Reference Market are rejected before publication. The previous registry, checksum, and configuration are restored exactly on any publication or focused-verification failure.
- An unsupported symbol such as `UNI-USD` continues to produce a typed non-directional `registry_not_configured` result with zero analyst/model calls; it cannot be made apparently supported by a successful refresh alone.
- Mainland registries, provider routing, qfq semantics, and Strategy Rules remain isolated and unchanged under ADR-0017, ADR-0019, and ADR-0021.

### Yahoo retry-budget ownership

- The Provider Request Coordinator is the sole policy owner for Yahoo retries. The Yahoo adapter performs one physical attempt per coordinated permit and returns a typed result; it does not run an invisible retry loop around one logical coordinator attempt.
- If a transport/library performs unavoidable internal retries, each physical attempt must invoke the same coordinator accounting and pacing boundary before network I/O. A library path that cannot expose or disable its attempts is not eligible for coordinated acquisition.
- Total physical attempts never exceed the configured coordinator budget. Attempt indices, timestamps, outcomes, cooldown changes, and pacing events are persisted by Yahoo's Upstream Service Identity and emitted to run telemetry.
- A success after retries reports the actual physical attempt count. Exhaustion reports a typed unavailable result with the same count; it does not start an additional adapter budget.
- Yahoo HTTP 429/`Too Many Requests` and valid `Retry-After` remain typed capacity outcomes that update shared cooldown state. Disconnects, empty frames, authentication failures, and malformed responses retain their distinct meanings.
- Identical concurrent requests remain single-flighted across the entire retry sequence. Fallback to another provider starts only after the selected provider's coordinated result is complete and remains sequential.
- Existing Operator Safety Ceilings, one-in-flight default, persisted cooldowns, priority order, and prohibition on fan-out or throttling probes remain exactly as required by ADR-0031.

### Runtime market-history gitignore rules

- Add a repository-root-anchored ignore rule for the configured default generated market-history runtime tree.
- The rule covers the main SQLite database, the derived payload-mutation sidecar lock database, their WAL/SHM or journal sidecars, the content-addressed payload tree, backups, temporary atomic-install/backup artifacts, and coordinator/maintenance state created inside that default tree.
- Do not broadly ignore the entire data directory. Intentional source fixtures, configuration, reports, logs governed by existing policy, and market data outside the exact default runtime tree remain visible to Git.
- Ignore rules do not delete, move, or untrack existing files. If a tracked placeholder or documentation file is intentionally kept in the runtime directory, add a narrow exception for it.

### Dependencies and delivery order

| Dependency | Required ordering or coordination |
| --- | --- |
| F1 -> F4 | Propagate authoritative status first (or in the same change) so v2 identity can commit to the status evidence actually used by the snapshot. The combined acceptance must not ship with v2 identity still encoding a lost `unknown`. |
| F4 -> F3 acceptance | OPIT and strict-replay acceptance must assert exact v2 membership; otherwise a provenance test could pass while pointing at a colliding legacy pin. The provenance logic itself may be developed independently. |
| F5 protocol decision -> F5 code | The prototype fixes the protocol as the global SQLite-sidecar payload-mutation mutex; F5 implementation may proceed only with the specified lock order, publication/GC ordering, timeout behavior, and deterministic test seams. |
| F4/F5 -> database and runtime finalization | Coordinate F4 snapshot-identity metadata with F5 runtime setup. F5 requires no main-database schema migration, but sidecar-path derivation, sidecar creation, backup/restore treatment, and default-runtime ignore coverage must agree on the canonical Market History Database path. |
| F7 -> F2 registry activation | Establish one supported-universe validator before pinning the shipped registry and enabling standard CLI resolution. |
| F2 registry pin -> F2 CLI graph | Resolve the pinned identity and asset configuration before constructing the graph, evidence state, checkpoint signature, or stream. |
| F6 -> crypto end-to-end acceptance | Yahoo attempt ownership must be correct before a Yahoo-backed Crypto Instrument run is used as final CLI/audit acceptance evidence. Deterministic crypto configuration tests may run earlier with a fake provider. |
| F8 | Independent and may land at any time, but its clean-worktree smoke test runs after all runtime acceptance scenarios. |

Recommended delivery sequence is: (1) F1 with the status-bearing portion of F4; (2) complete F4 exact identity/pins and F3 provenance; (3) implement the selected F5 SQLite-sidecar mutex protocol; (4) implement F7, registry pinning, and asset-before-graph CLI construction; (5) centralize F6 retries; (6) add F8; (7) run migration, CLI/audit, deterministic concurrency, and full-suite acceptance. Independent changes may be reviewed separately, but the final acceptance matrix is joint.

### Compatibility and migration

#### Existing snapshot IDs and pins

- Treat `snapshot:<64-hex>` as legacy v1 and `snapshot:v2:<64-hex>` as the new format. Readers accept both; all new publications use v2.
- Do not bulk re-key legacy snapshots or rewrite reports that reference them. A legacy pin remains readable only when its persisted parent and exact child membership are internally consistent and all referenced revisions/payloads verify.
- A legacy collision or incomplete membership cannot be reconstructed safely. Mark it typed unavailable/corrupt for strict replay; do not infer the intended second membership or bind it to a newly generated v2 ID.
- Reconstructing current evidence from proven exact legacy membership may publish a separate v2 snapshot while preserving the legacy record. The relationship must be explicit in audit lineage, not an in-place replacement.

#### Existing Market History Databases and provenance

- Any required schema migration is explicit, versioned, transactional, foreign-key checked, and compatible with WAL/full-synchronous operation. Failure leaves the pre-migration database usable and unchanged.
- F5 requires no main Market History Database schema migration. On first payload mutation, create or open the SQLite sidecar lock database at the path derived from the canonical main-database path; configuration aliases must resolve to the same sidecar, and a payload root shared by different canonical databases is rejected or unsupported.
- Do not relabel existing aggregate provenance in place. A corrected successor bundle may be published only when exact member provenance and timestamps prove the new classification; otherwise the conservative legacy classification remains.
- Opening an upgraded database must not trigger garbage collection or destructive repair. Run integrity checks separately from migration.
- A database row that references a missing payload is corruption, not an orphan. Current mainland analysis may enter observable History Store Degradation under ADR-0029; strict replay fails closed.
- Existing provider frames, calendars, revisions, cooldowns, leases, and backup manifests remain readable unless a typed integrity failure is found. Do not reset request state merely to adopt the retry fix.

#### Existing payload files

- Preserve the content-addressed directory layout and digest identities. No bulk move, rewrite, or recompression is required.
- Existing verified files and digest identities are reused in place; acquiring the new sidecar mutex must not rewrite them. Existing unreferenced rows/files become eligible for collection only through the implemented locked recheck/delete/commit/unlink protocol. Referenced bytes are never deleted as migration cleanup.
- The sidecar lock database is new runtime coordination state, not payload evidence. Its creation does not change existing snapshot IDs, pins, payload digests, database evidence rows, or reports, and backup/restore must not mistake it for a substitute for verifying the main database and referenced payload files.
- Backup/restore continues to verify the database and every referenced payload together.

#### Existing crypto configuration and checkpoints

- Standard installations gain the shipped pinned registry without requiring environment edits. Explicit custom overrides retain existing precedence but must provide a coherent path/digest pair and pass the same universe validation.
- Do not rewrite an invalid custom registry or checksum on load. Return a typed configuration result and require transactional republishing.
- Checkpoints whose signature does not prove the asset configuration/registry/calendar semantics are legacy. They may be read for diagnostics but are not resumed into a directional run without an explicit migration; the safe default is a new run.

#### Existing reports and decision audits

- Reports remain immutable historical records. Do not rewrite prior Current Tradeability, snapshot IDs, provenance, crypto horizons, or Yahoo attempt counts.
- New report/audit schema output identifies the snapshot-ID version, Current Tradeability and status provenance, exact pin-membership digest (when stored), provenance class, registry ID/digest, Reference Market, observation-calendar kind, rule horizon, and physical provider-attempt events.
- Legacy reports lacking these fields remain viewable but cannot satisfy the new acceptance criteria without a new deterministic run.

#### Existing runtime files and Git state

- The ignore rule changes only Git visibility for untracked generated state. It does not delete data or make an already tracked file untracked.
- Acceptance compares a clean worktree immediately before and after a default-path CLI run; pre-existing user changes are excluded from that comparison and never modified.

### Acceptance Criteria

| ID | Verifiable scenario | Required CLI and decision-audit result |
| --- | --- | --- |
| AC1 | Run a deterministic mainland fixture whose selected BaoStock bundle confirms the latest applicable session is suspended. | CLI completes with a non-directional Analysis Outcome, exact reason `instrument_currently_suspended`, no Buy/Hold/Sell/position/signal, no directional-memory write, and zero downstream model calls. Audit shows provider BaoStock, `current_tradeability=suspended`, authoritative status provenance or membership digest, the correct latest genuinely traded close, and a v2 snapshot ID. |
| AC2 | Run the same mainland path with an authoritative latest status of traded. | Current Tradeability is `tradeable`; the existing 20-trading-session rule and provider ordering are unchanged; no extra provider is called solely for status or replay preparation. |
| AC3 | Publish two snapshots with identical OHLCV but different trading-status revisions, then read both pins. | IDs differ; each pin returns only its exact ordered observation/status membership and factor/calendar set. Attempting conflicting membership under either ID fails atomically without changing the first pin. |
| AC4 | Seed an OPIT bundle, then publish an OPIT 21-session incremental refresh that omits older retained OPIT dates. | Latest reconstruction remains `observed_point_in_time`; audit membership includes retained plus refreshed revisions and the new v2 identity. |
| AC5 | Add a genuinely unseen historical date or include a retrospective member, and replay before/after a later correction's observation time. | Aggregate provenance remains `retrospective_backfill` when required; replay before the correction excludes it or fails closed, while a later eligible replay uses the exact new revision. |
| AC6 | Execute the complete deterministic GC/publication matrix defined below with independent connections, separate processes, and event barriers. | Every committed reference is readable and digest/length-valid; publication either succeeds completely or fails cleanly; GC never reports or deletes a concurrently committed digest; mutex timeout and unlink failure are typed and do not report successful collection; no sleep-based timing assumptions are used. |
| AC7 | Run `SOL-USD` through the real CLI boundary with deterministic provider/model fixtures and standard configuration. | No `registry_not_configured`; audit identifies the pinned crypto registry/digest, instrument kind `crypto`, `CCC` Reference Market, crypto Capability Profile, 21 consecutive daily closes including weekends, and horizon `20:calendar_days`. No equity `trading_days` horizon appears. |
| AC8 | Attempt custom refresh and CLI analysis for `UNI-USD`. | Refresh rejects the candidate before publication and preserves prior registry/checksum/configuration; CLI returns typed `registry_not_configured`, non-directional output, and zero model calls. |
| AC9 | Make a deterministic Yahoo transport fail retriably three times and succeed on the fourth within budget. | Exactly four network attempts occur; audit/coordinator records four physical attempts with per-attempt outcomes and pacing. One logical attempt or any hidden additional attempt fails acceptance. |
| AC10 | Exhaust a smaller Yahoo budget and separately return HTTP 429 with valid `Retry-After`. | Actual calls equal the configured cap, then a typed unavailable outcome is emitted. The 429 path persists shared cooldown and prevents an immediate probe; fallback remains sequential. |
| AC11 | Open representative legacy databases/reports containing v1 IDs, valid pins, invalid pins, OPIT/retrospective bundles, and payloads. | Valid legacy evidence remains readable, invalid exact membership fails closed, no IDs/reports are rewritten, no payload is deleted during migration, and any schema migration is atomic. |
| AC12 | From a clean worktree, run a default-path CLI scenario that creates the database, WAL/SHM files, payloads, backup/temp state, and audit reports. | Generated market-history runtime state does not appear in Git status; intentional audit/report behavior follows existing rules; negative-control source fixtures and configuration remain visible. |
| AC13 | Run focused regressions, the full supported test suite, lint, and whitespace/diff validation. | All pass, and review confirms no Strategy Rule change, no Supported Crypto Universe expansion, no provider reordering/fan-out, and no unrelated refactoring. |

## Testing Decisions

### Test philosophy and seams

- Test observable contracts at the highest existing seam. Use the real CLI callback/runner and compiled graph for terminal behavior, the Authoritative Market Snapshot acquisition boundary for provider propagation, the public Market History Store publication/reconstruction API for identity/durability, the shadow history publication boundary for incremental provenance, the registry refresh/load boundaries for configuration, and the Provider Request Coordinator with a fake transport for attempts.
- Prefer deterministic provider frames, registries, clocks, model clients, and transports. No regression test requires live market data or a paid model.
- Assert terminal contracts, audit fields, persisted membership, physical call counts, and durable bytes. Do not assert private helper call order except where a documented concurrency linearization point is itself the contract.
- Reuse existing prior art: current-tradeability preflight tests, provider-fallback snapshot tests, exact-pin strict-replay tests, incremental shadow-refresh tests, payload orphan/backup/migration tests, crypto asset-mode and registry rollback tests, coordinator single-flight/cooldown tests, and CLI symbol-handling tests.
- New synchronization hooks are allowed only at the highest store publication/collection boundary needed to make concurrency tests deterministic. Keep them private and inert in normal runtime; do not introduce a general event framework.

### Regression coverage for every finding

| Finding | Required regression tests |
| --- | --- |
| F1 | (a) A real accepted BaoStock suspension candidate reaches snapshot/evidence/audit with `suspended` and the correct latest traded close; (b) the compiled graph/CLI stops before model nodes and emits the ADR-0023 reason; (c) a blank/zero-volume row without authoritative status is not promoted; (d) History Store Degradation does not erase status; (e) a confirmed suspended candidate is not bypassed through later fallback. |
| F2 | (a) Standard config/package load pins and resolves `SOL-USD` from any working directory; (b) partial/mismatched pin config fails typed before model work; (c) CLI constructs crypto mode before checkpoint/stream creation; (d) audit and registered calculation use 21 calendar-day observations including weekends and `20:calendar_days`; (e) CLI/programmatic contracts match; (f) mainland 20-trading-session behavior is unchanged. |
| F3 | (a) OPIT seed plus partial OPIT overlap remains OPIT; (b) omission of older dates has no classification effect; (c) a genuinely unseen historical date remains backfill; (d) any retrospective contributing member prevents upgrade; (e) retrieval-cutoff tests exclude later revisions; (f) input/order permutation is invariant. |
| F4 | (a) Status-only revision changes v2 ID; (b) exact identical membership is stable under ordering permutations; (c) exact republish is idempotent; (d) conflicting parent, observation/status pair, factor, calendar, or provenance under one ID fails atomically; (e) deleted membership/revision/payload fails strict replay; (f) valid v1 read compatibility and invalid-v1 fail-closed behavior. |
| F5 | (a) Ordinary orphan collection; (b) same-digest republication after row deletion/before unlink, immediately before unlink, and after unlink/before mutex release; (c) publisher interruption before final reference commit; (d) two concurrent GC workers; (e) different-digest publication during GC; (f) unlink failure and eventual cleanup/reuse; (g) separate-process termination and automatic mutex release; (h) typed mutex timeout with no mutation; (i) concurrent and repeated same-digest publication remains idempotent; (j) bundle, provider-frame, calendar, and exact-snapshot publication paths hold the mutex through all durable references; (k) a missing/invalid file for a committed row fails closed as corruption; (l) backup/restore remains valid. |
| F6 | (a) N fake transport attempts equal N coordinator physical-attempt records; (b) success after retries; (c) exact budget exhaustion; (d) 429/Retry-After shared cooldown; (e) non-capacity failures retain type; (f) identical concurrent requests single-flight across retries; (g) sequential fallback; (h) no nested adapter retry when one permit is granted; (i) audit count equals transport count. |
| F7 | (a) Unsupported base/quote candidate rejection; (b) alias quote substitution rejection; (c) no file/config mutation after rejection or focused-test failure; (d) shared policy admits every and only Supported Crypto Universe member; (e) runtime returns typed unsupported outcome with zero model calls. |
| F8 | (a) Git ignore checks for the default database, WAL, SHM, payload, backup, and temp paths; (b) negative controls outside the exact tree remain unignored; (c) clean-worktree before/after default CLI smoke; (d) existing tracked files are not removed. |

### Deterministic payload-GC and snapshot-publication tests

These tests are mandatory for the selected sidecar-mutex protocol. The suite must use independent main-database and sidecar connections, include separately spawned publisher and GC processes against the same canonical database/payload root, and coordinate named operation boundaries with `threading.Event`, `multiprocessing.Event`, or an equivalent deterministic barrier. Sleeps, polling delays, and elapsed-time races are forbidden; a bounded timeout may fail a hung test but may not be the evidence that exclusion occurred.

Provide one private, default-no-op phase hook at the public store publication/collection boundary, equivalent to `phase_hook(phase, digest)`. It must be inert in normal runtime and expose at least these deterministic phases:

- `publisher_mutex_acquired`
- `publisher_file_installed`
- `publisher_payload_row_committed`
- `publisher_references_committed`
- `gc_mutex_acquired`
- `gc_row_delete_committed`
- `gc_before_unlink`
- `gc_after_unlink_before_mutex_release`

The required deterministic cases are:

1. **Ordinary orphan collection:** Collect an unreferenced registered digest. The final state is `row absent, file absent`, the result reports collection only after unlink, and a repeat pass is idempotent.
2. **Republication after row deletion and before unlink:** Pause GC at `gc_row_delete_committed`, start same-digest publication from an independent connection/process, and prove with events that it cannot pass mutex acquisition or mutate the file/database. Resume GC, then require publication to complete idempotently with a verified committed row and file.
3. **Republication immediately before unlink:** Pause at `gc_before_unlink` and repeat the same-digest publisher assertions. GC may unlink before releasing the mutex; the publisher may only verify/reinstall and commit after release.
4. **Republication after unlink and before mutex release:** Pause at `gc_after_unlink_before_mutex_release`. Assert the safe intermediate state `row absent, file absent`, prove the publisher remains excluded, then release GC and require a complete verified republication.
5. **Publisher interruption before final reference commit:** Pause separately at `publisher_file_installed` and `publisher_payload_row_committed`; prove GC cannot enter while publication holds the mutex, then roll back or terminate the publisher before `publisher_references_committed`. Reopen the store and prove that any unregistered/unreferenced verified residue is collectible or reusable and that no durable reference points to missing bytes.
6. **Two concurrent GC workers:** Use two workers with independent connections/processes for the same digest. Only one may hold the global mutex; after release the other rechecks current rows/references and skips or completes idempotently without a duplicate success or unsafe unlink.
7. **Different-digest publication during GC:** Pause GC for digest A and start publication for digest B. Assert the deliberately conservative global serialization, bounded release, typed timeout behavior when injected, and correct final state for both digests. This test documents that per-digest concurrency is not part of this fix.
8. **Unlink failure:** Inject failure before physical removal. Require the typed retryable result and safe residue `row absent, file present`; a later locked sweep must remove it, or a later publisher must verify and reuse it. GC must not report the failed attempt as collected.
9. **Separate-process termination:** Terminate a GC process after `gc_row_delete_committed` and, separately, after unlink at `gc_after_unlink_before_mutex_release`. Prove SQLite releases the sidecar mutex automatically, the respective safe residues are recoverable, and a new process can publish or collect without stale-owner recovery state.
10. **Idempotent republication and concurrent publishers:** Before and after every blocked same-digest interleaving, republish identical bytes and the same durable reference. Also run two publishers for valid separate references to the same bytes. Successful operations resolve to one verified content-addressed payload without duplicate/conflicting references.
11. **All high-level publication paths:** Run the interruption and GC boundary matrix through complete Provider History Bundle, provider-frame, and calendar publication APIs. Each must retain the same mutex from payload inspection/installation through its final durable reference commit; no lower-level helper may release it between those phases.
12. **Exact snapshot publication under collection:** Publish raw observations, trading statuses, factors, calendar data, the complete bundle, and its v2 pin while GC runs independently. On success, reconstruct the pin and verify every exact revision membership, payload digest/length, provenance field, and Current Tradeability. On failure, assert no partial pin or durable dangling reference.
13. **Corruption, timeout, and recovery controls:** Seed a committed payload row with a missing or invalid canonical file and require publication/read/strict replay to fail closed without repair. Separately force sidecar-mutex acquisition timeout and assert a typed result with byte-identical database and filesystem state. Re-run backup/restore verification with the main database and every referenced payload; the sidecar file must not mask missing evidence.

### Verification stages

1. Run the focused tests for each area before combining storage/schema changes.
2. Run the deterministic CLI/audit scenarios with fake providers and fake model clients; assert zero model calls for deterministic blockers.
3. Run legacy migration/compatibility fixtures and backup/restore verification before any live smoke test.
4. Run the full test suite and lint checks required by the repository, plus whitespace/diff validation.
5. Perform Spec Compliance review first: ADR-0023, ADR-0031, registry isolation, observation calendars, exact PIT provenance, compatibility, and all acceptance IDs.
6. Perform Code Quality review second: transaction boundaries, lock ordering, crash recovery, typed errors, audit completeness, regression coverage, and absence of unrelated refactoring.

## Out of Scope

- Reopening the diagnosis, collecting more live execution evidence, or changing production code as part of writing this specification.
- Changing or expanding Strategy Rules, predictive logic, position sizing, profitability claims, or model prompts unrelated to asset configuration.
- Expanding the Supported Crypto Universe, adding executable crypto venues, substituting stablecoin quotes, or unifying the crypto and mainland registries.
- Reordering the Current Analysis Provider Chain, adding provider fan-out, calling another provider after a complete candidate solely for replay preparation, or changing Data Usage Mode.
- Inferring suspension from volume/blank rows without authoritative evidence.
- Replacing SQLite, redesigning the content-addressed payload layout, or moving garbage collection into foreground analysis.
- Bulk rewriting legacy snapshot IDs, pins, databases, payloads, checkpoints, or reports.
- Automatically deleting or moving existing runtime market-history files after adding ignore rules.
- Broad cleanup, module renaming, dependency upgrades, formatting churn, or unrelated refactoring.

## Further Notes

- This specification treats F1-F8 as confirmed investigation evidence and intentionally starts at required behavior rather than repeating the diagnostic record.
- `ready-for-agent` authorizes implementation of all specified work, including F5. The authoritative prototype artifacts resolve the payload-GC design gate in favor of the global SQLite-sidecar payload-mutation mutex; no implementation design decision remains open.
- If implementing the selected F5 protocol or a required compatibility constraint conflicts with an accepted ADR, stop and record a new ADR decision rather than silently overriding the existing one.
- A controlled live provider/model run is not required for acceptance. If one is proposed later, it requires the normal explicit cost/network authorization and does not replace deterministic tests.

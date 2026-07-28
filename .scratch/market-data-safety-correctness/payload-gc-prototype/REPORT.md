# Payload garbage-collection design decision

Status: prototype complete; design gate resolved

## Verdict

Select a **single cross-process payload-mutation mutex implemented by a tiny
SQLite sidecar lock database**. Every operation that can install, register,
reference, deregister, or unlink a payload must acquire this mutex before any
payload-file or payload-metadata mutation. The mutex is held through the final
publication reference commit or, for GC, through metadata deletion commit and
canonical-file unlink.

The mutex uses `BEGIN IMMEDIATE` on a lock database whose path is derived
deterministically from the canonical Market History Database path. SQLite's
writer lock supplies cross-process exclusion and is released automatically
when the connection closes or its process terminates. No lease expiry,
tombstone recovery, generation counter, or platform-specific file-lock library
is required.

GC should acquire/release the mutex per candidate digest, not around the
initial candidate scan or an entire sweep. This bounds foreground publication
delay while retaining one simple global ordering rule. Different digests are
conservatively serialized for their short mutation sections; the prototype
shows the waiting publisher completes idempotently after release.

## Prototype evidence

The prototype used real temporary SQLite databases, real temporary payload
files, independent connections, one publisher, one or two GC workers, and
named `threading.Event` barriers. It ran 45 matrix cases: five protocols by
nine interleavings. It also ran two separate-process probes for the selected
protocol, including forced GC-process termination after metadata deletion
commit and before unlink.

| Protocol | Immediate invariant failures | Eventual invariant failures | Decision |
| --- | ---: | ---: | --- |
| Delete row, commit, unlink, recheck | 2 | 2 | Reject |
| Owned tombstone with phases and quarantine | 0 | 0 | Safe, but reject on complexity |
| Generation check immediately before unlink | 1 | 1 | Reject |
| Main metadata transaction held through unlink | 1 | 0 | Reject |
| SQLite sidecar payload-mutation mutex | 0 | 0 | Select |

Both selected-protocol separate-process probes passed:

- republication immediately before unlink was excluded by the sidecar writer
  lock, then completed idempotently after GC released it;
- terminating the GC process after row deletion commit and before unlink left
  `row absent, file present`; SQLite released the lock automatically, and the
  publisher safely reused the file and committed a verified live row.

Two complete reruns produced byte-identical `results.json` with SHA-256
`8B270A5B6147C6AF6864B92B351A8BCF6A5FE33743B195513D760C9E4026DE7F`.
The exhaustive per-protocol/per-interleaving states and outcomes are in
`RESULTS.md`; the machine-readable source is `results.json`.

Runtime used for the prototype: Python 3.13.7, SQLite 3.50.4, Windows 11. The
protocol relies only on SQLite and filesystem behavior already required by the
repository, not on Python 3.13-specific features.

## Why the other protocols were rejected

### Delete, commit, unlink, then recheck

This reproduces the confirmed race. If republication commits after row
deletion but before unlink, GC removes the newly republished canonical file.
The final recheck detects the damage but cannot restore bytes or roll back the
publisher's committed reference. It failed both the broad after-deletion
window and the immediately-before-unlink boundary, and recovery correctly
refused to collect the now-referenced row, leaving the invariant broken.

### Owned tombstone, quarantine, and finalize

The fully fenced version passed every case, so it is not rejected for safety.
It loses on production complexity: it needs a schema migration, owner tokens,
phases, a quarantine path, publisher blocking semantics, idempotent takeover,
and proof that a stale owner cannot resume after fencing. A simple tombstone
without those pieces is unsafe with two GC workers. The sidecar mutex produces
the same serialization and crash safety with automatic OS/SQLite lock release
and no durable recovery state.

### Generation/publication token checked before unlink

The check closes only the earlier part of the window. A publisher can commit a
new generation after the check and immediately before unlink. GC then deletes
the same canonical digest path. Avoiding that TOCTOU would require exclusion or
generation-specific physical paths; exclusion is the selected mutex, while
generation-specific paths would weaken the existing content-addressed layout
and deduplication model.

### Main metadata transaction held through unlink

The main SQLite writer transaction excludes concurrent publishers during the
normal operation, but filesystem unlink is not transactional. If unlink
succeeds and the process stops before commit, SQLite rolls back the row
deletion while the filesystem deletion remains. The durable state is a live
row without a file. Committing before unlink avoids that rollback problem but
reopens the original race unless a separate exclusion mechanism spans both
steps.

## Exact production invariants

1. A committed `payload_artifacts` row implies that the canonical payload file
   exists and verifies against its digest and byte length.
2. One canonical sidecar lock database is derived from one canonical Market
   History Database path. All processes using that store resolve the same lock
   path, and one Market History Database exclusively owns its configured
   payload root.
3. Every payload mutator acquires the sidecar `BEGIN IMMEDIATE` mutex before it
   reads, installs, registers, references, deregisters, sweeps, or unlinks a
   canonical payload. No production publication path may call payload install
   and later publish references across separate mutex scopes.
4. Lock order is always sidecar payload-mutation mutex first, main Market
   History Database transaction second. No code acquires them in reverse.
5. Publication holds the mutex from before canonical-file inspection/install
   until every durable row that references the digest commits. It releases the
   mutex only after verifying that the committed row and canonical file agree.
6. GC's initial candidate list is advisory. For each digest, GC acquires the
   mutex, starts `BEGIN IMMEDIATE` on the main database, and rechecks all
   reference tables. A referenced or missing candidate is skipped without file
   mutation.
7. For a proven orphan, GC deletes the payload metadata row and commits while
   still holding the sidecar mutex, then unlinks the canonical file before
   releasing the mutex. Therefore a publisher cannot recreate the row between
   commit and unlink.
8. The unregistered-file sweep applies the same per-file mutex and main-database
   recheck. A registered file is never deleted from a stale snapshot of the
   registered-digest set.
9. A publisher that acquires the mutex first either adds/reconfirms its durable
   reference before release or leaves at most an unreferenced row/file. A GC
   that acquires next must recheck and skip the referenced digest.
10. Publication of identical verified bytes and the same durable reference is
    idempotent: it does not change content identity, create duplicate
    references, or replace a verified canonical file.
11. If GC commits row deletion and unlink fails or the process stops before
    unlink, the safe residue is `row absent, file present`; a later locked sweep
    can remove it, or a publisher can verify and reuse it.
12. If GC stops after unlink, the safe residue is `row absent, file absent`.
    SQLite releases the sidecar mutex on connection/process termination.
13. If publication stops after file installation but before metadata/reference
    commit, the safe residue is an unregistered file. If it stops after all
    commits, the file already exists and verifies.
14. An existing committed row whose canonical file is missing or invalid is
    corruption, not an orphan or silent repair opportunity. Current analysis
    degrades observably and strict replay fails closed under the existing ADRs.
15. Mutex acquisition timeout is typed. A timed-out publisher performs no file
    mutation and reports a history-store write failure/degradation; a timed-out
    collector leaves state untouched and retries during later maintenance.
16. GC reports a digest as collected only after canonical unlink succeeds or
    the canonical path is already absent while the mutex is held. An unlink
    error is retryable maintenance failure, not successful collection.

## Production pseudocode

### Publication

```text
publish(payload_bytes, durable_reference_rows):
    digest = sha256(payload_bytes)

    with acquire_payload_mutation_mutex(canonical_store_lock_path):
        existing = read_payload_artifact_metadata(digest)

        if existing exists:
            require canonical_file(digest) verifies digest and byte length
            require existing metadata equals proposed metadata
        else:
            atomically_install_and_fsync(payload_bytes, canonical_file(digest))
            verify canonical_file(digest)

            BEGIN IMMEDIATE on Market History Database
            insert-or-validate payload_artifacts row
            COMMIT

        BEGIN IMMEDIATE on Market History Database
        publish all revision/bundle/frame/calendar rows and their payload refs
        validate idempotent conflicts exactly
        COMMIT

        require committed payload row exists
        require canonical file still verifies

    return published identity
```

The existing separate payload-registration and reference-publication
transactions may remain if the same outer mutex spans both. A bare
`install_payload` operation may release after installing an unreferenced
artifact, but a high-level publisher must not release between installation and
its reference commit.

### Garbage collection

```text
collect_orphans():
    candidates = advisory scan of unreferenced payload rows and unregistered files

    for digest/path in candidates:
        with acquire_payload_mutation_mutex(canonical_store_lock_path):
            BEGIN IMMEDIATE on Market History Database
            current = re-read payload row and every reference table for digest

            if row is absent and this is a registered-row candidate:
                COMMIT
                continue

            if any durable reference exists:
                COMMIT
                continue

            if payload row exists:
                delete payload_artifacts row
            COMMIT

            try:
                unlink canonical path (missing is idempotent success)
            except unlink error:
                record retryable maintenance failure
                continue

            record digest as collected
```

For the physical sweep, the canonical path from disk is only a candidate. The
row/reference recheck under the same mutex decides whether unlink is legal.

## Deterministic regression-test seams

Use one private, default-no-op phase hook at the public store
publication/collection boundary:

```text
phase_hook(phase, digest)
```

Required phases are:

- `publisher_mutex_acquired`
- `publisher_file_installed`
- `publisher_payload_row_committed`
- `publisher_references_committed`
- `gc_mutex_acquired`
- `gc_row_delete_committed`
- `gc_before_unlink`
- `gc_after_unlink_before_mutex_release`

Tests use `threading.Event`/multiprocessing events and two independent store
connections; no sleeps. The minimum deterministic cases are:

1. Pause GC after row deletion commit and before unlink; start same-digest
   publication; prove it cannot mutate until release, then succeeds and
   verifies.
2. Pause GC at the exact pre-unlink phase and repeat the same assertion.
3. Pause GC after unlink but before mutex release; prove publication remains
   excluded and then reinstalls/commits idempotently.
4. Pause publication after file install and after payload-row commit; prove GC
   cannot enter until the final reference transaction commits or rolls back.
5. Run two GC workers; prove only one holds the mutex and the second rechecks
   after release.
6. Run GC for digest A and publication for digest B; document conservative
   blocking, bounded release, and correct final states for both.
7. Inject unlink failure before removal; prove row absence/file presence is
   safe and a later locked sweep removes or a publisher reuses the file.
8. Terminate a separate GC process after metadata deletion commit/before
   unlink, and separately after unlink; prove automatic lock release and safe
   recovery states.
9. Assert publisher idempotence before and after every blocked interleaving.
10. Run the same scenarios for bundle, provider-frame, and calendar publication
    paths so no caller releases the mutex between payload registration and its
    durable references.

## SQLite and filesystem assumptions

- The metadata database and lock database are local SQLite files on a
  filesystem that honors SQLite cross-process locks. Network filesystems with
  unreliable advisory locking are unsupported.
- `BEGIN IMMEDIATE` on the sidecar lock database obtains the sole writer mutex
  even when the lock transaction performs no data change; the prototype
  verified this across spawned Windows processes.
- SQLite releases the sidecar writer lock when its connection closes or its
  process terminates. No stale lock row or lease-expiry recovery is needed.
- The main database retains `foreign_keys=ON`, WAL mode, `synchronous=FULL`, a
  bounded busy timeout, and `BEGIN IMMEDIATE` for orphan recheck/deletion and
  publication commits.
- Canonical payload installation uses a temporary file in the destination
  directory, flush/fsync, same-filesystem atomic replacement, and digest/length
  verification before metadata can commit.
- File unlink removes the directory entry atomically or reports failure. A
  failed unlink leaves a reusable/collectible unregistered file.
- Every process derives the same canonical lock path; configuration aliases or
  two databases sharing one payload root are rejected/unsupported.
- The prototype proves process-concurrency and process-interruption behavior,
  not arbitrary storage-device failure beyond the repository's existing
  SQLite `FULL` and atomic-install guarantees.

## Specification amendment

The ready-to-insert amendment is captured in `SPEC-AMENDMENT.md` beside this
report. It replaces the unresolved design-gate paragraph while retaining the
protocol-independent invariants and deterministic test requirements.


### Payload garbage-collection concurrency — design gate resolved

The throwaway deterministic prototype selected a **global cross-process
payload-mutation mutex implemented by a tiny SQLite sidecar lock database**.
Its path is derived from the canonical Market History Database path. Every
publisher and collector acquires `BEGIN IMMEDIATE` on this sidecar before any
payload-file or payload-metadata mutation. The lock is acquired before the
main Market History Database transaction and is released automatically when
the connection closes or its process terminates.

Publication holds the mutex from before canonical-file inspection or atomic
installation through the commit of the payload row and every durable row that
references it. Existing separate payload-registration and
reference-publication transactions may remain only when the same outer mutex
spans both. A committed payload row must always have a canonical file that
verifies by digest and byte length; a missing file for an existing row is
corruption and is never silently repaired.

GC's initial candidate scan is advisory. For each candidate digest, GC
acquires the mutex, begins an immediate transaction on the Market History
Database, rechecks every payload reference, skips referenced/missing rows, and
commits deletion of a proven-orphan payload row. While still holding the
sidecar mutex, it unlinks the canonical file and only then releases the mutex.
The physical unregistered-file sweep uses the same per-file lock and database
recheck rather than a stale snapshot of registered digests. GC reports success
only when unlink succeeds or the path is already absent.

This ordering makes the safe failure residues explicit:

- publisher interruption before reference commit leaves at most an
  unregistered or unreferenced verified file/row;
- GC interruption after row deletion commit but before unlink leaves `row
  absent, file present`, which a later locked sweep can remove or a publisher
  can verify and reuse;
- GC interruption after unlink leaves `row absent, file absent`;
- unlink failure leaves `row absent, file present` and is a retryable
  maintenance outcome.

Lock order is always sidecar mutex first and main database second. Mutex
timeouts mutate nothing: publication returns a typed history-store write
failure/degradation and GC retries during later maintenance. Candidate
digests are handled in separate short mutex scopes so foreground delay is
bounded. Different digests are conservatively serialized; changing to
per-digest locking requires new evidence rather than complicating this fix.

The prototype evaluated 45 protocol/interleaving cases plus two
separate-process probes. The selected mutex and a fully fenced tombstone design
had zero invariant failures. The mutex was selected because it needs no main
schema migration, tombstone phases, fencing tokens, quarantine recovery, or
platform-specific file-lock dependency. Delete/commit/unlink/recheck failed
two republication windows; a generation check failed the final TOCTOU window;
holding the main database transaction through unlink failed when interruption
rolled back the row deletion after the file was gone.

Regression tests must use independent connections and deterministic event
barriers at: publisher file installation, payload-row commit, final reference
commit, GC row-deletion commit, immediately before unlink, and immediately
after unlink before mutex release. They must cover same-digest republication at
every boundary, two GC workers, different digests, idempotent publication,
unlink failure, and separate-process termination. Sleeps are not permitted.

Assumptions: the metadata and sidecar databases are local SQLite files with
reliable cross-process locking; all processes derive the same sidecar path;
one Market History Database exclusively owns its payload root; the main
database retains foreign keys, WAL, `synchronous=FULL`, bounded busy timeout,
and `BEGIN IMMEDIATE`; and canonical installation retains same-directory
atomic replacement plus digest/length verification.


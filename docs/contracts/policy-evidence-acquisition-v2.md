# Bounded evidence acquisition and authenticated intake v2

This is an evidence transport/acquisition contract, not a policy adoption or
qualification grant. Existing claim, revocation, invalidation, stage ordering,
raw equality, historical exception and conditional-consumer contracts remain
unchanged. New candidate bytes cannot inherit an earlier exact-source approval.

## Complete bytes and compact consent

The primary transport is ordered **chunks on the existing native owner channel**.
The service never opens a caller-supplied evidence path. `transfer_evidence.py
stage` reads a caller-local file, validates its complete schema/semantic identity
and whole-file pin, and sends at most 1 MiB per chunk. The native owner checks
the closed descriptor, exact instance, offset, total length and complete SHA-256.
The maximum logical artifact is 256 MiB; compression is not assumed. Every
artifact remains one complete observation or document, never independently
accepted root fragments.

Native `evidence-import-begin-v2`, `evidence-import-chunk-v2`, and
`evidence-import-finish-v2` are **staging only**. Bytes reside in bounded pending
files under the owner's existing `state/temp`; completed content is published
once to `artifacts/<sha256>.blob`. The immutable completion marker is published
after complete bytes. A missing marker, partial file, cancellation, changed
length/digest, stale offset or concurrent second writer cannot be admitted.
An interruption may retain pending files, orphan published bytes, or a completed
unauthenticated transfer. None is authority. There is no automatic retry,
resume, history deletion, migration or operational-store cleanup.

`policy-owner-evidence-intake-v2`, kind `artifact-reference`, binds the transfer
ID and `policy-owner-artifact-descriptor-v2`. The descriptor binds:

- complete artifact length and SHA-256, separate canonical semantic identity,
  schema and identity field;
- exact instance, registered role, approval, candidate commit/tree/parent,
  lineage plan, acquisition identity, stage, phase and requested operation;
- actual proposed issue/expiry timestamps with at most one-hour validity.

A path, digest, descriptor or staged object alone is not authority. The native
frontend displays the exact descriptor, full bound artifact path, byte length
and digest for complete inspection. No abbreviated preview substitutes for the
artifact. Genuine separate human review and Windows Hello remain required to
register the document. Complete native byte checks run before the challenge
and again before accepting its response. The ordinary native signature chain,
principal/role checks, owner protections and durable original journal remain.

The Python owner independently validates the complete logical artifact and
context before deriving its immutable registration. Historical authenticated
records are re-derived against their immutable recorded time; this does not
rewind the durable clock or permit a new expired request. Consumers resolve
registered semantic references and verify complete byte pins. The bounded
`evidence-read-chunk-v2` route checks registration before reading content.
`transfer_evidence.py export` verifies the full resulting digest and publishes
to a new output only after complete readback; failures retain a `.partial` file.

## Capacity and compatibility

| Bound | Value |
| --- | ---: |
| Complete artifact | 268,435,456 bytes |
| Chunk | 1,048,576 bytes |
| Descriptor | 16,384 bytes |
| Pending imports | 8 |
| Transfer records | 4,096 |
| Store logical content/reservations | 8 GiB |
| Import/descriptor lifetime | 3,600 seconds |
| Native control frame, unchanged | 67,108,864 bytes |
| Complete authentication journal, unchanged | 33,554,432 bytes |
| Prospective authentication-envelope reserve/maximum | 262,144 bytes per new record |
| Native transfer child | 30 seconds; 256 MiB committed memory |
| Heavy Python validation child | 300 seconds; 4 GiB committed memory |

The private native transfer child requires the exact registered executable and
dedicated service SID. Ordinary clients cannot invoke it as a substitute for
the registered owner. Jobs bound owned children only. Large JSON parsers and
comparison consumers still materialize full documents: streaming transport is
**not an end-to-end constant-memory claim**. Capacity exhaustion is explicit and
blocking; increasing memory or deadlines is not automatic.

The actual cumulative journal is checked before issuing each human challenge,
reserving the bounded record envelope as well as the exact document. The actual
serialized journal, envelope and complete worker request are checked again
before publication. Prior records are never rewritten to fit. The narrowly
scoped `evidence-capacity-v2` native read reports current/projected byte totals;
it does not query ledger contents/lifecycle state, issue a challenge or reserve
capacity. `preflight_evidence.py --check-registered-capacity` optionally uses
that read only from the exact installed depot. Without it, current registered
journal capacity is explicitly UNKNOWN; staged projections are not a claim
about an installed journal or future unknown documents. Concurrent subsequent
registrations still require the pre-challenge and publication checks.
Normal anchor verification still opens the ledger object for its identity/ACL
metadata; that protection check is retained.

The private semantic validator returns exact document/purpose/instance binding
and the intersection of already required descriptor/approval validity bounds.
Native acceptance time is sampled after expensive validation, checked against
that window, and kept unchanged in the record. The current time is checked again
immediately before publication after serialization. Expiry crossed during
validation therefore rejects before an immutable record can become
underivable; no earlier validation clock or extended expiry is substituted.

Compatible small v1 artifact intake remains available, with original limits and
records unchanged. v2 descriptors cannot be wrapped as v1 or mixed into a v1
batch. Unsupported schema/version downgrade fails closed. Operational
`current-source` and new `observation` registrations now require v2 producer
provenance. Earlier TEST-only fixture assertions remain TEST-only. Raw opaque
logs use the explicit `policy-evidence-bytes:v1` byte-digest schema; they are not
silently reinterpreted as structured stage evidence.

## Real producers and independent verification

`acquire_evidence.py produce-source` requires explicit repository, expected
candidate document and fresh output. It uses an explicitly selected existing
Git executable and its SHA-256. This
local Git installation is an external bootstrap prerequisite; it is not hidden
inside or implicitly supplied by the minimal owner Python depot. Its observed
path, pin and version are retained, and no ambient PATH Git is selected.
The inventory binds the complete reviewed Git tree, commit object and parent,
physical tracked files and the production Python
module namespace. Existing source-manifest/tree validation is reused. Physical
SHA-256 values remain separate from Git canonical bytes; an explicitly declared
UTF-8 CRLF checkout representation is checked rather than concealed.
`verify-source` independently rechecks proof, coverage, actual physical bytes
and extra executable/import files. Neither command authenticates its own trust.
The compact inventory descriptor and v2 current-source assertion subsequently
need the existing trusted human intake route. A candidate's own assertion,
TEST enrollment or old qualified runtime is insufficient.

`acquire_evidence.py capture` requires an explicit
`policy-evidence-root-list-v1` document, phase, fresh output and (for AFTER) the
exact prior BEFORE. It reads actual opened Windows objects, holds ancestors,
rejects reparse/hardlink substitution, hashes complete file bytes and retains
raw attributes, times and requested security-descriptor hashes. It never masks
`0x10000000`, changes a historical object, rewrites a baseline, or silently drops
an inaccessible row. Missing/unstable/incomplete observations remain visible
and cannot report successful equality. New collection times, nonce, method,
collector/runtime pins and coverage are separate from historical root rows.
The original retained pair is never relabelled as a fresh acquisition.

`compare` performs complete root comparison. The owner binds the new capture
and its separately authenticated observation to the exact allocation, plan,
measurement contract, stage and phase. Canonical claim-time admission still
compares the exact original AFTER to a distinct new BEFORE and enforces all
atomic single-claim/current-authority conditions.

## Composed route and human gates

1. A new exact-source deployment and genuine role/policy/execution decisions
   must exist before operational acquisition. Setup alone grants none.
2. Run delivered fixed-binding/capacity `preflight_evidence.py`; it performs no
   owner RPC, authentication, allocation, namespace initialization or claim.
3. Produce and independently verify the actual source inventory. Stage complete
   bytes, inspect them, authenticate the descriptor and exact current-source
   registration. Obtain canonical allocation through the existing contract.
4. At the authorized acquisition time, collect the full additive bridge, stage
   it and authenticate its descriptor and exact observation registration.
   Only then may the existing atomic claim path check and consume authority.
5. For ISO02, ISO03, QCF17, ISO04/Ticket06, independent reviews and closure, use
   the existing procedures. Stage their complete canonical artifacts through
   the same route; authenticate required observation/node/provenance documents
   in their actual dependency order. A verifier is not a substitute stage run.
6. Existing finalization emits the conditional result and separate receipt.
   Export complete referenced bytes with bounded readback. No legacy-ready
   downgrade, Ticket-07 preparation or live/provider/model authority exists.

Dates, future allocation IDs, storage object IDs, native authentication records
and exact future document hashes cannot be fabricated during preparation.
Authentication fixtures in tests deliberately have an invalid native record
contract; they are not Windows Hello success and cannot enroll or approve an
operational owner. Headless native tests exercise the real transfer class but
do not prove the service-account/UI path. Genuine registered-owner validation
and later separately approved qualification remain human gates.

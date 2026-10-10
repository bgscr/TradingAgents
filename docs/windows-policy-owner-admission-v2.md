# Same-instance enrollment admission v2 candidate

This candidate implements `ENROLLMENT_ONLY`. It is a preparation artifact, not a
deployment, recovery result, platform qualification, policy decision, or human
authentication. Actual build identities, review findings and test outcomes belong
to the separately sealed preparation packet. An absent platform validation report,
maintenance cut or final activation receipt makes the formal entry fail closed.

The development baseline and business candidate remain
`33337b24b5aa07d11c5b9c29485f8fb3604fc2e9`. The new implementation has its own
commit and build pins. Original C remains `STOP_PARTIAL_STATE_PRESERVED`; the
subsequent capture A remains `STOP_PARTIAL_CAPTURE_PRESERVED`. Neither the old
native exit status nor old credential existence is inferred from a new test.

## Identity contract

| Identity | Binding and treatment |
| --- | --- |
| Historical setup | Complete immutable `policy-owner-storage-binding-v1`, including its original native and worker pins; preserved verbatim inside the v2 binding |
| New implementation | New source commit, build ID, new native byte pin and complete new worker manifest byte pin |
| Business candidate | The unchanged 40-character historical candidate above |
| Original role | Exact 1,912 bytes, SHA-256 `4bb794118689e9d8d5090c5d6ab16dec8173203c98173f38e63e94d7bc22d70c`; identity `policy-owner-project-role-proposal:v1:sha256:6dd4906ba66a270f74b6aad7f1ec95dc6239ee50339bb61c061971dbe8a060a0` |
| Instance and principal | Same owner root, instance, service name/SID, operator SID, ledger identity, root object ID, owner DB object ID and ledger DB object ID |
| Admission basis | Durable cut identity and full classified-state evidence identity, included in the signed challenge and recomputed against the migration row |
| Frontend | Actual connected pipe client PID, creation FILETIME and pinned new native image, with actual operator/elevation checks |
| Attempt | Durable epoch and fresh 256-bit nonce, under one owned physical gate/lease |

The v2 binding is a closed canonical object with fields `contract_version`, `mode`,
`legacy_setup_binding`, `implementation`, `business`, `preserved_objects`,
`control_schema`, and `identity`. Its identity namespace is
`policy-owner-admission-binding:v2`. The implementation commit is supplied by the
build; source files do not pretend to contain their own Git hash.

The original role names the old business package and candidate. It is displayed in
full, along with the three separate identity sets. No role text, signature or
target candidate is substituted. A verified old record remains a v1 record and is
verified against its old binding/native pin. Its signature is never carried onto
the new implementation binding.

## Formal entry and build closure

The native owner entry accepts only `service-v2` and `enroll-v2`. Grammar rejection
precedes path initialization, registry access, anchor construction and WebAuthn.
The v2 build excludes the old service, setup and transfer source files. Compile
guards remove the old generic RPC client, old human flow, conditional owner,
runtime initialization/prepare/inspect/run implementation, and v1 credential
creation/probe entry. They are absent from the compiled v2 assembly, not merely
selected away by a request boolean.

The conditional host and standalone runtime entry reject before reading inputs.
The full source archive retains historical source for review; it is not the
executable worker import closure. The new depot contains only the six listed v2
Python source files and the preserved sealed Python runtime. Ordinary owner,
ledger writer, reconciliation, policy, provider, qualification and application
modules are not deployed in that closure. The `tradingagents` namespace is created
without executing its normal package initializer or `.env` loader.

`owner_worker.py` rejects old or unknown frames before project imports. A formal
frame has exactly `contract_version`, `mode`, `anchor_v2`, `lease_id`, `operation`
and `payload`. Public pipe operations are inspect, reserve, begin creation,
marshalled creation-boundary observation, accept and complete verification. The
closed-gate lease adoption operation is private to service startup. Migration is
available only through the separate maintenance coordinator, never through the
ordinary pipe/worker RPC.

The TEST native assembly is compiled with a separate symbol and entrypoint. Its
WebAuthn boundary cannot fall back to the Windows authenticator. The formal binary
contains no TEST hook. Synthetic capability construction does not grant production
worker authority; formal construction requires actual OS registration, token,
code, object, boot, activation and lease checks.

## Maintenance cut and migration protocol

The maintenance script defaults to a plan-only result. Execution requires the
exact pinned new depot/interpreter, the actual elevated registered operator and a
protected, purpose-bound `AdmissionV2MaintenanceAuthorization`. That record binds
the implementation, operator, one step, expiry, finite budget and the exact
authorizing document. This development task does not issue such a record.

The two steps have separate authority:

1. Prepare a new protected control directory and owned gate. Persist the guard
   change intent. Use SCM to set only the existing service's start type to disabled
   and its image command to the new pinned owner. Preserve account, SID and type;
   reject unqualified recovery commands/actions, triggers or delayed startup.
   Require the exact original image/manual-start configuration before changing it;
   an unknown or altered service is not taken over or repaired.
   Read actual SCM configuration back and save a protected guard receipt containing
   the observed old kernel boot identity. Stop without stopping/starting a service
   or rebooting the machine.
2. After separately authorized full kernel reboot, revalidate the protected guard,
   new kernel identity and still-disabled exact new startup configuration. Hold
   the same owned gate while capturing the complete authoritative durable state.
   SQLite sidecars, unknown objects, incomplete copies, mismatched identities,
   changed ledger contents or uncertain conditions stop this step. Only a completed
   ELIGIBLE classification can create v2 tables in the existing owner DB.

Classification precedes the `LEGACY_STATE_CLASSIFIED` receipt. Its evidence binds
the copied DBs, immutable baseline, original role, authentication files, artifact
inventory/schema and temp state. The ledger is compared read-only against its
fixed baseline. No ledger connection is ever opened for writing. Existing valid
v1 authentication and derived closure result in an external existing-context stop;
partial or invalid closure results in BLOCKED. Neither initial outcome changes the
owner DB, role, old registration or binding.

An eligible migration records `SCHEMA_MIGRATED_CLOSED`, stages the new separate
registration, verifies that actual registration, and records
`V2_ACTIVATED_CLOSED`. Only then may the same coordinator set demand startup for
the new image. A final activation receipt contains actual startup readback, cut
identity and a CLOSED/no-attempt state. The coordinator publishes its pin into its
own previously staged registration and verifies the readback. A registration with
a missing/null activation pin is not accepted by the service. Intermediate state
never reverts automatically to v1.

The protocol uses a pinned platform profile and actual raw validation evidence for
the exact OS build. `NtQuerySystemInformation` boot layout and the own-worker
`NtQueryInformationProcess` parent layout are version-sensitive. The implementation
requires qualified sizes, successful return statuses, a nonempty changed boot ID,
and rejection of hibernation/soft-boot flags. No profile or proof of those platform
semantics is issued by synthetic tests. BCD boot-entry IDs, SCM STOPPED, a reboot
claim and a process scan do not substitute for the protocol. The worker queries
its own process for parent provenance and does not enumerate the process table.

## Continuous ownership and closed handoff

The native service holds the existing new `owner-gate.lock` with read/write access
and only shared read, plus one unique immutable lease file, across all RPCs and
the entire human ceremony. It keeps those handles while a worker performs the
small SQLite transactions. No SQLite transaction is held open during Hello.

Each worker independently checks the actual native parent PID/creation/image,
service token, protected registration and lease object. It duplicates the parent's
exact gate and lease handles, checks their object IDs and keeps the duplicates
until worker termination. Thus parent death does not release the sharing barrier
ahead of an in-flight worker. The formal parent/Job/duplicate-handle behavior still
requires platform validation; synthetic tests do not establish it.

A different service generation may adopt only an ELIGIBLE,
`V2_ACTIVATED_CLOSED`, CLOSED row with no attempt and an empty attempt table. Actual
OS evidence must establish that the prior lease is released while the new owner
holds the unique gate. The adoption CAS increments epoch and remains CLOSED.
Active phases, UNKNOWN/partial state, BLOCKED state and any used generation cannot
be adopted for authentication. No previous process is reopened for termination.

## Credential generation and actual parameter boundary

The new handle is exactly 32 bytes:

`SHA256(UTF8("TA.PolicyOwner.H2.v2") || 0x00 || canonical({rp, operator_sid, instance, attempt_nonce}))`.

It maps to the same Windows/business principal and uses a separately durable
credential generation. The nonce and handle are unique in the append-only attempt
table. At most one attempt is admitted by this candidate. Failure, timeout or
uncertainty never obtains another generation. There is no old-credential enumerate,
delete, export, recover or reuse operation.

The frontend requires a durable `CREATION_BEGUN` acknowledgement before preparing
the native API call. After constructing the actual WebAuthn structs, it reads back
the unmanaged `user.Id`/`cbId` and client-data buffers and sends that observation
through the same authenticated frontend RPC. The service validates the exact
attempt/epoch, intent receipt, frontend creation instance, RP, handle, ES256,
platform attachment, required UV and NONE attestation, then publishes the
observation before acknowledging it. Only afterward can the fixed frontend call
the authenticator. A lost acknowledgement never retries the creation call.

The observation is labelled `MARSHALLED_BEFORE_CALL`. It is not a claim that the
authenticator created a credential. NONE attestation does not independently prove
the user handle. The fixed native code, actual process provenance and boundary
observation form that trust path; actual Windows Hello/credential behavior remains
a separate platform validation requirement.

## Persistent state and acceptance point

All normal transitions use `BEGIN IMMEDIATE` and compare epoch, attempt, phase and
lease. Stale requests reject without rewriting the active attempt. Existing clock
values cannot go backward, original rows are preserved, and new immutable rows are
append-only. The challenge signs role/business identity, new and old bindings,
frontend generation, nonce/handle, deadline, epoch and the cut/classified-state
identities. Every later durable effect rechecks that admission basis.

| Phase | Durable meaning; interruption treatment |
| --- | --- |
| CLOSED | Migration complete but no attempt; only explicit reservation can proceed |
| RESERVED | Epoch advanced, unique generation, challenge and deadline committed; cancellation consumes the opportunity |
| CREATION_BEGUN | Creation intent committed before native boundary/API; no retry even if no credential result is returned |
| PUBLICATION_INTENT | Independently verified full record bytes committed; no accepted file yet is implied |
| Atomic authentication publication | **v2 acceptance point**: complete immutable `authentication/00000000.json` appears by write-through, no-replace move after flushed private temp bytes |
| RECORD_PUBLISHED | Control row acknowledges publication; a preceding crash may leave a valid accepted file with incomplete derived state |
| BLOB_PUBLISHED | The identical immutable record blob exists; no index row is inferred |
| INDEXED | One v2 artifact reference is committed; no applied row is inferred |
| APPLIED | One enrollment-only applied response is committed; full closure still requires verification |
| COMPLETE_STOP | Full authentication/crypto/blob/index/applied/control closure verified and final epoch advanced; only verification remains |
| BLOCKED | Partial, expired, crashed, mismatched or uncertain state is retained; no automatic repair, reopening, retry or recreation |

Publication temp creation, flush, atomic move, each SQLite commit, handoff and block
boundaries are explicit synthetic fault points. Temp/partial evidence is retained.
If a crash occurs before the control acknowledgement of a published record, the
record can be an accepted fact while the complete enrollment context is not yet
established. The system does not replay effects automatically.

The independent verifier uses raw ES256 proof, exact client data, RP hash, UV/UP,
credential ID/public key and counter rules. The service also uses its independent
native CNG verifier before submission. There is no trusted
`signature_verified=true` payload. V1 references retain their exact canonical JSON
plus LF bytes. New v2 references have their own exact canonical representation;
old rows are never normalized or overwritten.

## Exact prospective write set

| Object | Permitted change by a separately authorized phase |
| --- | --- |
| `<same root>/admission-v2/` | Stage new implementation, preserved runtime, native binaries, manifests and immutable baseline references; new object ACLs only |
| `<same root>/state/admission-v2/` | New protected gate/unique leases, guard/boot/copy/classification/migration/activation receipts and boundary observations; preserve partial files |
| Existing service configuration | Same account/name/SID/type; disabled/new-image guard, then demand/new-image only after closed activation; no start, stop or reboot in this coordinator |
| Existing protected owner registration key | New `AdmissionV2StagingJson`, purpose-bound maintenance authorization and `AdmissionV2Json`; do not rewrite old `AnchorJson` or its old files |
| Existing `owner.sqlite3` object | Add `admission_v2_*` control/migration/attempt/event schema and only the bounded enrollment rows in `artifacts` and `applied_records`; ordinary SQLite rollback journals may be transient |
| Authentication/artifact directories | At most the new first v2 authentication file and its identical content-addressed blob, only after ELIGIBLE migration and a valid attempt |
| Existing `state/temp` | New owned publication temps only; an interrupted temp is retained, not cleaned |
| New frontend/service processes | Only their own process/token/default-child DACLs for the declared query/protection boundary |

Forbidden writes include the original ledger and its identity, original DB object
replacement, original data/role/old authentication rewrites, original root/DB ACL
repair, credential deletion, old package modification, setup rerun, rename/reinstall,
cleanup, policy/current-source/113-root B0/qualification/Ticket-07/live state.

## Validation and release boundaries

The isolated batch is declared and frozen once, with at most 900 seconds total
and 60 seconds per actual chain including cleanup. An unexpected failure stops
later chains and forbids fix-and-rerun. Planned fault outcomes are evaluated as
such. The old eight Job cases are not rerun; their exact helper bytes are reused
only for owned-process boundary operations. New controller integration is reviewed
and reported separately. Synchronous native/file calls have measured cooperative
budgets; no hard-timeout claim is made for a blocked OS call.

The batch covers safe imports/syntax, pre-import v1 rejection, synthetic state/DB
concurrency and persistence cases, fake RPC, actual marshalled WebAuthn buffers at
a TEST interception, and independent in-memory CNG checking of a public synthetic
key/proof. It also inspects the formal binary metadata for absent v1/TEST entry
implementations. It does not start a service, access installed owner state, mutate
the registry/ACLs, reboot, or invoke Hello.

Before any deployment, a separate isolated Windows VM campaign must establish the
eight profile controls: full kernel boot versus resume/fast startup, disabled
startup across boot, actual service SID/token, registry/file ACLs (including the
unchanged `0x10000000` comparison), duplicate-handle lease lifetime, own-worker
parent query, genuine WebAuthn handle behavior, and SQLite durability/complete cut
copies. The VM needs the exact candidate artifacts, exact supported OS build,
separate synthetic instance/data/account, snapshot retention, local platform
authenticator support, and permission for the explicitly planned SCM/ACL/reboot/
Hello operations. It must have no real installed instance, provider credentials or
live network application path. A passing simulation cannot issue that profile.

After platform qualification, staging/deployment, maintenance guard, full reboot,
post-boot copy/migration, context-only verification and a new human authentication
require separate concrete authorization. No maintenance/migration/authentication
command for immediate execution is included in this development delivery.

Microsoft describes the NT information API as version-sensitive and recommends
runtime resolution; this candidate treats an unsupported layout/status as closed.
See [NtQuerySystemInformation documentation](https://learn.microsoft.com/en-us/windows/win32/api/winternl/nf-winternl-ntquerysysteminformation).

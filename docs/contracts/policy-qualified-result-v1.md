# Prospective policy-qualified result contract v1

This is inactive source support for `policy-qualified-execution-result-v1`.
It is not exception approval, a replacement-policy decision, qualification,
Ticket 06 FINAL PASS, Ticket 07 preparation, or execution authority. Retained
BLOCKED outcomes and the unresolved historical Important finding stay intact.
The exact candidate, policy, manifest and one-lineage plan must be approved
separately after their final identities are known. No prior candidate approval
transfers to a successor commit or tree.

## Authority and trust ownership

`PolicyQualifiedVerifier` has no default owner or ledger. It refuses activation
without an application-supplied `TrustedPolicyOwner` and that owner's existing,
registered `LineageLedger`. Neither can be supplied through result JSON, a flag,
an environment variable, an identity hash or a guest PASS. The owner is the same
trusted readiness/coordinator role that already supplies independently verified
current and qualified source bindings to production preparation.

The owner must authenticate the approving person, immutable decision artifact,
channel/session and issuer entitlement. Its fresh `AuthorityResolution` must
establish all of these independently:

- The authentic originating policy permits this prospective acceptance, or an
  evidenced issuer entitled to supersede it has enacted a prospective replacement.
  The unavailable originating text must not be reconstructed from blocker summaries.
- The campaign issuer replaced its prospective historical-admission instruction,
  and the project specification owner adopted this conditional outer contract.
- The exact reviewed policy/package, exception scope, candidate and one plan were
  explicitly approved, with current non-revocation and a trusted UTC clock.
- Qualification execution has separate, current authority for the exact candidate
  and plan. This is not downstream analysis/live/packet authority.
- Current source and coordinator/consumer/procedure/measurement bindings still
  equal the approved candidate and complete plan; the registered ledger is intact.

`confirm_policy_decision_challenge` uses the existing protected handoff's exact
identity then literal `YES` interaction. It returns no approval or principal.
An authenticated channel owner must retain and authenticate the decision. There
is no new identity provider, signing service, global bypass, or installed resolver.
The dataclass returned by the owner is a process trust interface, not a serialized
authentication certificate. A caller that controls that trusted process/owner can
already falsify readiness; content hashes do not defend against that threat.

## Historical and new evidence

`HistoricalExceptionScope` retains the original candidate separately from the
proposed successor candidate, the BLOCKED artifact and byte pin, original
before/after acquisitions and whole-file pins, one exact root and root identities,
the complete delta ledger, raw values, row count, collector/procedure pins,
collection durations and explicitly unavailable observation/sample/transition
times. Equality is always false and causation remains UNRESOLVED.

Eligibility authenticates the original acquisition registry, recomputes saved
content identities and whole-file pins, reconstructs every changed ledger row
from the full before/after inventories, and compares every root. Only the exact
approved root, directory rows, field and raw value pair may differ. There is no
path pattern or allowed-bit mask, including for `0x10000000`. Copies and newly
acquired byte-identical observations cannot impersonate the original pair.

New observations are separately registered to the claimed lineage, stage,
phase and measurement contract. A comparable new-start bridge must start at the
retained original after inventory. New stage before/after raw inventories and
their bridges must be strictly equal to the retained after roots. A changed
before/after pair cannot become eligible merely because its two new snapshots
match each other. Missing, contradictory or incomparable observations fail.

The host finalizer reads actual canonical source/runtime/ISO-03/ISO-04 evidence.
It invokes the existing staged-source and full offline-runtime verification,
ISO-03 report validator, Ticket 06 command/result validators, bounded export
validator (including last-write completion), and canonical host inventory
comparator. It binds the qualified runner, sealed English environment, source,
runtime, baseline, process and export identities. The unchanged exact 27-command
ledger and QCF17 clause A AND current-run clause B precede any QCF18 acceptance.
A failed command, process gate, review or new canonical/raw delta remains fatal.

The owner must register execution records only from the actual separately
authorized coordinator, with immutable artifacts and authentic acquisition
provenance. Parsing reports cannot prove a native observation happened. The
tests deliberately provide a labelled synthetic owner, clocks and evidence;
they establish contract behavior, never native containment or qualification.

Future qualification requires independent **Standards and Spec** review records,
with zero new qualification Critical/Important findings. These are distinct from
the evidence-integrity and authorization/consumer reviews of this implementation.
The historical Important count is retained separately in the qualification record.

Pre-result closure requires new, registered post-review canonical and raw
protected comparisons. The canonical comparison bridges from ISO04's retained
after inventory; new acquisition identifiers, lineage/stage/phase and UTC bounds
prevent an earlier observation from substituting for the post-review capture.
It also requires a complete physical evidence inventory over the claim's exact
pre-result evidence root and all five earlier stage roots (ISO02, ISO03, QCF17,
ISO04/Ticket06 and reviews). Source/runtime/export paths must stay under their
claimed stage roots. Every file and directory is included, without filename
exclusions; inaccessible directories, reparse entries, missing predecessors,
extra files and changed bytes fail closed. The inventory is checked again at
finalization and downstream use. Its own file and all later closure/result/receipt
files are published only outside those sealed roots, in the distinct closure
allocation. This explicit predecessor scope avoids a self-reference and cannot
be narrowed in a claim. Test mutations use only synthetic fixture copies.

## Consumption and sealing order

The reviewed policy hashes predecessors only. A completed
`ReviewedPolicyPackage` then binds those exact policy bytes. An authenticated
`OperatorApproval` binds that package afterward. The claim deadline is exactly
seven days after effectiveness; the terminal deadline is exactly thirty days.
Both upper bounds are exclusive. No implementation/test clock starts a real
approval period.

The plan contains the canonical execution and cleanup budget for every stage,
plus a finite final verification/receipt allowance. Fresh execution authority
must attest those exact budgets and its own expiry. Before claiming or starting
a stage, all remaining stages, cleanup and closure must fit strictly before both
the policy and execution-authority deadlines. Completed stage work is not charged
again as future execution, but each stage completion must be within its own
recorded start-to-completion budget. Final verification has its own elapsed-time
bound. These numbers require separately reviewed canonical procedure/budget
authority; the implementation supplies no real values or extra allowance.

The canonical owner first supplies a sealed `LineageAllocation` containing only
the exact proposed names, roots, baseline and plan bindings. It has no claim
timestamp or consumption assertion. The pre-claim bridge binds that allocation
and plan, with no future lineage hash. After checking its evidence, current full
authority and remaining budget, `claim` creates the `LineageClaim` with the latest
owner UTC timestamp and consumes it atomically. Callers must use this returned
claim. An observed seven-day expiry permanently blocks an unclaimed plan; a
duplicate request cannot expire an already consumed lineage's valid thirty-day
window. This separates acquisition/allocation from actual durable claim timing.

The trusted owner explicitly provisions a fresh protected ledger before approval.
Admission opens an existing registered ledger only; absence/conflict is failure.
SQLite `BEGIN IMMEDIATE`, unique approval/plan keys and append-only records reserve
one exact claim and consume its single attempt before ISO02. Stage start/completion
records enforce order and reject repeated/concurrent starts. Failure never frees
the reservation. The materialized claim must equal the canonical allocation
owner's exact stage IDs/roots, evidence root and next unused additive baseline
version. The ledger also permanently reserves those IDs/roots/version, rejects
overlapping roots and prevents their reuse by a different approval or plan.
Reopening can read a consumed claim but cannot retry or recreate
it. Observed revocation/expiry is recorded permanently, including after a result;
a later ACTIVE response cannot resurrect the plan. Unknown authority fails closed.
Every substantive lifecycle write and receipt registration checks permanent
invalidation inside the same SQLite transaction as its append. Concurrent
revocation between authority resolution and storage cannot admit a new start,
completion, terminal result or receipt. Additive terminal-failure recording remains
permitted; immutable earlier evidence is retained.
Protected ledger storage and the owner's coordinated fresh checks remain required;
SQLite does not defend against an administrator rolling back the storage volume.

The order is:

```text
policy -> approval -> canonical allocation -> pre-claim bridge -> exclusive claim
ISO02 -> ISO03 -> additive QCF17 baseline -> ISO04/Ticket06
    -> independent Standards/Spec reviews -> comparisons/inventory -> pre-result closure
    -> terminal conditional result -> separate acceptance receipt
    -> downstream envelope -> conditional preparation/attempt/report
```

Every execution/review/closure node binds the same immutable acceptance binding
and only earlier evidence. The terminal result contains the full policy,
approval, claim, historical caveat and execution chain. Its status is exclusively
`CONDITIONALLY_ACCEPTED_UNRESOLVED_HISTORY`. The result-derived core includes the
result byte pin and all acceptance/history/candidate/lineage/expiry data, with
`execution_authorized=false`. It contains no receipt or downstream reference.
The subsequent receipt binds that core, closure and authority-check reference.
The ledger durably records its exact identity and bytes separately. A forged,
missing, stripped, or unrecorded receipt fails downstream validation. No hash
includes itself or a successor.

## Consumers and compatibility

The actual producer interface is `claim` -> `begin_stage`/`complete_stage` ->
`finalize` -> `issue_receipt`. The caller persists immutable returned bytes in
the owner's artifact store. A crash after terminal consumption cannot start
another qualification; a missing artifact stays unavailable. These methods
perform admission/evidence work and never allocate or launch native stages.

Stage completion validates every available predecessor and exact source/runtime,
runner, host, baseline and export binding before it can advance. Starting the next
stage revalidates already completed evidence against its consumed ledger entries.
Finalization uses these same validators. Current complete authority is resolved
again immediately before lifecycle writes and final acceptance; an earlier clock
sample cannot backdate a claim, stage start, result or receipt past expiry. Checks
that observe expiry/revocation stop further admission; previously authorized safe
cleanup/export remains the existing execution owner's obligation, not a new grant
from this module.

`verify_result`, `verify_downstream`, `report`, routing preparation sealing and
validation, `bind_production_preparation`, `derive_attempt` and
`validate_conditional_attempt` all require fresh owner resolution for conditional
use. Their identities cover the entire envelope; no identity-only projection is
accepted. Data-model constructors perform structural validation only and confer
no authority. The production source variants must still match the readiness
owner's independently verified complete current source.

Legacy v1 source/preparation/attempt models and identity namespaces retain their
original semantics. They reject new conditional fields and namespaces. New
conditional models use distinct contract versions and namespaces; there is no
downgrade or migration function. Replacing a prefix or stripping fields cannot
match the current conditional source. Legacy opaque identities still depend on
the legacy owner's existing independent verification; this change does not make
arbitrary strings evidence of a valid legacy qualification.

The current C# host and protected handoff remain unaware and reject the new
attempt version. Python attempt derivation is contract support only. There is no
conditional native launch support, real Ticket 07 readiness, packet or live
authorization in this change. Crash protection, ancestry/TOCTOU, Plan 1.5/CNINFO,
temporal retrieval, shadow non-authority, disabled `qualified_v1`, provider/model,
retry and checkpoint behavior retain their existing contracts.

`contract_descriptor()` exports the closed schema graph and its
`policy-qualified-result-contract:v1:sha256:…` content identity. Whole-file SHA-256
and length remain separate pins. New JSON entry points reject duplicate keys,
unknown fields and nonfinite numbers. A policy must also bind the reviewed source
implementation; a schema hash alone does not pin validator behavior.

## Offline verification and remaining activation decisions

`scripts/test_policy_qualified_offline.py` runs explicitly selected tests with an
existing interpreter under `-I -B -S`. It reads existing dependency files without
processing editable/site hooks, removes live credentials, disables plugin
autoload, blocks child processes/network access, and confines writes and synthetic
SQLite state to a fresh external `offline-*` directory. No install, native test,
real historical scan, qualification, stage allocation or protected-state write is
authorized by this harness. Unfiltered repository tests require their own native
and environment isolation authority and are not a substitute for these checks.

Before any qualification, the precise decisions in the authority section, a new
exact-candidate reviewed package/plan, authentic operator approval and separate
bounded execution authorization are still required. The new candidate decision
draft is unsigned and NOT_APPROVED, stored outside its commit after commit/tree
identities exist. Original historical Important findings remain unresolved and
separate from implementation review counts.

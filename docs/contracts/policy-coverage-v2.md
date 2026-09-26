# Prospective historical-coverage contract v2

Inactive implementation support, not policy adoption or qualification. A distinct
exact-source v2 policy and genuine approval are required. Existing v1 models and
strict original-root equality retain their semantics and reject v2 documents.

## Four independently executable relationships

| Relationship | Required predicate | Forbidden interpretation |
|---|---|---|
| Original BEFORE -> original AFTER | Existing exact registry/whole-file/canonical/root/complete delta-ledger verification; only the exact approved 2,571-row attributes delta; equality=false, cause UNRESOLVED | Equal history, completed historical acquisition, repaired prior BLOCKED |
| Original AFTER -> B0 | Complete scope manifests derived from all original roots and every failure/reparse record; every known recorded state field preserved; explicit accounting for acquisition-only/unavailable fields and each permitted newly observed row | Convenient intersection; missing known paths; object-identity or hidden-content continuity |
| B0 admissibility | Distinct genuinely new acquisition, exact candidate/instance/scope/measurement/method/runtime/time binding, every declared root present, complete required security metadata, zero acquisition errors; only declared supported link objects with independently established containment | An old copy or digest as authority; an omitted path called complete; unknown as equal |
| B0 -> every later current observation | Exact complete measured root objects under the same versioned scope, including physical object/volume identities, raw values, descriptors, file bytes and bounded link data/relationships | Returning to impossible original-root equality; allowing new differences because history was uncertain |

Scope manifests preserve the seven error markers separately from same-path
directory rows, and all 5,322 reparse rows with their full saved data and positions.
Fixture scopes have the same closed shapes but conspicuous fixture authority and
different saved bytes. Counts, directory prefixes and path wildcards are not scope
authority. Expansion below an exact historically unenumerated directory must be
explicitly inventoried in B0 and in the transition assessment, then frozen for the
new window. A new/undeclared reparse in that expansion is not permitted.

All raw state keys present in an ordinary saved row remain comparable. Failure
kind/error codes describe acquisition failure, not object contents; retain and
report them separately. Attributes/timestamps recorded only on an error marker
remain required known fields of the same named directory. Historical root counts,
requested coverage and identity values are accounted for as acquisition metadata;
old and new hashes stay distinct. Original missing object IDs, security descriptors,
descendants and link tag/target data are permanently UNKNOWN, never backfilled.

## Bounded native link-object measurement

The initial implementation supports explicitly declared Windows mount-point
reparse buffers (directory junctions), tag 0xA0000003, on local drive paths only.
Other tags, symbolic links, cloud/offline placeholders and volume mount targets
are unsupported and fail closed. Support is a measurement capability, not an
exception for executable/runtime/authority/artifact paths.

Open the actual link object with OPEN_REPARSE_POINT, retain ordinary ancestor
handles and no-write/no-delete link handle, read bounded FSCTL_GET_REPARSE_POINT
data, and verify the opened object against the named object before/after. Preserve
raw bytes/tag, object/volume identity, raw attributes and security metadata.
Substitute/print names must be unambiguous, local and consistent. The target must
be a separately held ordinary directory reached by a bounded, component-by-component
no-reparse walk from the held declared root. Both target and root handles must
have matching volume and stable identities. Prefix strings alone prove nothing.
Maximum indirect-link depth is zero: a link target/ancestor that is itself reparse
or cyclic is rejected. No implicit target traversal or permission bypass occurs.

The ordinary root walk independently inventories required target contents. Link
objects do not supply that coverage; the target relationship must bind an ordinary
directory actually present in the completed inventory. All new/unlisted, missing,
escaping, dangling, ambiguous, unsupported, changed or incompletely measured links
remain blocking. Directory nesting, entry/byte/time/memory and link-buffer limits
are finite. No ACL repair, backup privilege, hydration or administrator request.

## End-to-end authority and caveat

Generated scale vectors use `GENERATED_FIXTURE_NOT_AN_OBSERVATION`, a distinct
fixture-acquisition identity and `read_bytes=0`, with logical represented bytes
reported separately. They are supported only with an explicitly fixture scope
in a TEST owner. Operational mode rejects them. Actual native disposable captures
remain separately identified as `NEW_OBSERVATION` with measured read bytes. Test
authority and synthetic stage vectors cannot substitute for physical observations.

A v2 policy binds the entire coverage scope and measurement artifacts by semantic
identity AND whole-file pin. A v2 exact decision/approval binds those same references.
Compact authenticated descriptors continue to reference immutable complete bytes;
large scope/inventories never return to inline authentication records.

An independently acquired canonical host state can have the same complete bytes
as an earlier state. The explicit `policy-owner-evidence-intake-v3`
`artifact-acquisition` record is keyed by the sealed descriptor identity, while
the immutable artifact retains its original semantic identity and whole-file pin.
A v3 observation supplies `artifact_descriptor_identity`; it must resolve that
exact already-authenticated acquisition, stage, phase, instance and plan. The
descriptor must be authenticated before its dependent observation. Same-batch
unapproved resolution, changing dates to renew an acquisition, original-acquisition
reuse, conflicting content pins and old policy/wrapper reinterpretation reject.
V1/v2 registration behavior stays unchanged. Native full-artifact validation,
human review text and final signing-time validity checks also include v3 records.
Identical content is never reused consent or proof of a new measurement.

One authoritative coverage validator supplies the bridge, all stage baselines,
review/closure checks, final result, separate receipt and downstream consumer.
Every v2 stage/review/closure record binds the same scope, measurement, B0 and
transition assessment. The terminal caveat states KNOWN_RECORDED_FIELDS_MATCH,
HISTORICAL_COVERAGE_UNKNOWN, HISTORICAL_OBJECT_CONTINUITY_NOT_ESTABLISHED and strict
CURRENT_WINDOW_PRESERVATION. It retains the original raw-difference caveat too.
Stripped fields, wrong versions, mismatched scope/baseline or v1 approvals reject.
No unconditional ready-v1 or live authority is emitted.

Atomic one-claim consumption/invalidation, stage ordering, all 27 commands,
QCF17 A AND B, independent reviews, ordinary sealed evidence roots, deadlines,
source/runtime ancestry and conditional-to-legacy rejection are unchanged.

## Test matrix and classification

The executable seams are scope derivation/validation; known-field transition;
current inventory validation/equality; real native disposable capture; delivered
CLI; compact descriptor and isolated-owner intake; all six stages through separate
receipt and native/read-only consumer validation. Negative cases cover omitted
markers/paths/known fields, hidden known drift, incomplete current security/data,
wrong scope/original/instance/candidate/version, every disallowed link relationship,
alias/substitution/cycles, missing caveats and old approval/downgrade attempts.

Classifications must remain separate: IMPLEMENTATION_PASS, FIXTURE_FEASIBILITY_PASS,
REAL_ENVIRONMENT_NOT_MEASURED, HUMAN_AUTHENTICATION_PENDING,
QUALIFICATION_NOT_AUTHORIZED. Real historical readability, supported current link
tags/containment and future authentic decisions cannot be inferred from fixtures.

Native API primary references:
- https://learn.microsoft.com/en-us/windows-hardware/drivers/ddi/ntifs/ns-ntifs-_reparse_data_buffer
- https://learn.microsoft.com/en-us/windows/win32/api/fileapi/nf-fileapi-createfilew
- https://learn.microsoft.com/en-us/windows/win32/fileio/reparse-point-operations

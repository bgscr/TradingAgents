# Windows policy owner and conditional native integration v1

This prospective project contract implements the operational boundary for
[policy-qualified-result-v1](policy-qualified-result-v1.md). It changes no historical
measurement, BLOCKED result, raw `0x10000000` comparison, or mandatory qualification
predicate. Implementation, setup and test evidence confer no policy approval or
qualification/live authority.

## Identity and prospective authority

The concrete local owner uses the existing Windows Win32 WebAuthn API with Windows
Hello platform authentication and required user verification. The native frontend
shows the complete exact document, then requests a real human gesture. The service
checks the client SID, elevated integrity and protected frontend image, creates a
single-use challenge, verifies the ES256 proof, and records its full provenance.
The challenge binds purpose, instance, storage, principal, document bytes and a
ten-minute validity window. Neither ambient SID, administrator rights, a public
key supplied by a client, a hash, nor a typed YES authenticates the operator.

New registrations derive a domain-separated 32-byte WebAuthn user handle from
the RP, operator SID and verified owner instance. Distinct test instances therefore
do not reuse the predecessor's RP/user-handle pair, which discoverable authenticators
may otherwise replace. Existing credentials and assertions remain bound to their
retained credential IDs; this prospective change migrates no enrollment.

The observed OS account is a proposed principal until the human confirms its
exact setup and enrolls it. Enrollment separately attests three prospective project
scopes: replacement policy for this personal-research campaign, campaign/specification
adoption, and the project's versioned conditional consumer contract. The issuer's
statement is retained as authority evidence for these scopes; Windows authenticates
who made it, not an entitlement over unidentified third-party or higher-level rules.
Those broader powers are expressly not claimed.

An exact package decision must reference the enrolled role and registered storage,
and repeat those same scopes. The package approval and bounded qualification
execution are separate explicit selections and bindings in the signed document.
The execution proposal has its own exact candidate, plan, budgets and time window.
The native owner emits an approval only after authentic acceptance. No materializer
selects APPROVE or starts a real effective clock. Rejection supplies no authority.

## Additive setup and protected storage

The setup helper prepares an external bundle from explicitly supplied existing
Python 3.13, local dependency and CSC inputs. It downloads and upgrades nothing.
The worker depot includes `packaging`, required by the canonical source/runtime
validators, in addition to Pydantic and its dependencies. Before emitting a setup
action, preparation runs a fresh staged-interpreter check of all worker validation
imports with filesystem writes, database access, subprocesses and network operations
denied. This is an import-closure check, not an authentication or qualification test.

An additive TEST setup may also carry an explicitly supplied, byte-pinned
`test-client-dependencies` subtree for the real inert client/CLI producer. It is
copied under the same protected static-depot DACL; it is never added to the
trusted worker's import paths. Human TEST execution uses the new installed
interpreter, source and client dependencies, not mutable staging or host packages.
Preparation checks complete source/runtime/dependency membership and exact pins.
The separate `--check-deployment` mode validates staged bytes only; it cannot
prepare an action, contact an owner or invoke authentication. Exact additional
client-native image pins do not enlarge native-consumer platform admission.

The reviewed TEST deployment configuration binds the exact setup proposal,
prospective instance, immutable binaries/manifest, and client payload. It contains
no fabricated future storage-object identity or enrollment receipt. After genuine
setup/enrollment, native status and the exact retained enrollment/role/artifact
chain establish those actual bindings. A prepared TEST package retains them and
the run must match them. Each child instance argument is covered by its exact
authenticated inert grant; operational namespaces and transferred enrollments
remain ineligible. Time-sensitive TEST proposals are generated only after these
prerequisites; setup preparation starts no policy approval clock.
Build/source pins are rechecked while handles prevent their modification. Native
setup independently pins the complete bundle and checks its closed file set,
exact database schemas, empty control tables, unset clock, artifact index and
original-registry references before showing the setup confirmation.

The human pastes a bootstrap with the exact reviewed action digest. It reads the
action once, verifies that in-memory byte array, and executes those same bytes.
The action explicitly imports the Windows PowerShell Management and Utility
manifests from `$PSHOME`, with session-wide command visibility for the built-in
`Get-FileHash` function's dependencies. It changes neither `PSModulePath` nor the
automatic-loading preference and needs no manually preloaded module.
The action emits its small Win32 interop stubs in memory and holds the reviewed
native executable against writes/deletion plus all ancestors against replacement
through its use. The first setup assembly executes from those verified bytes in
the existing elevated, 64-bit Windows PowerShell 5.1 STA host; a writable external
`exe.config` or CLR startup sidecar therefore cannot intervene before its entry
point. Enrollment then launches the registered executable from its protected
depot. No compiler output or mutable helper DLL is loaded by the bootstrap.
The staging manifest uses the intended frozen source
bytes as its pins and rejects unexpected files or staging rewrites; it never adopts
a changed staging file as a new expected executable/runtime identity.

Only a human's visible confirmation may create a new instance:

- Service: `TA.PolicyOwner.{test|operational}-YYYYMMDD-NNN`, manual start, own process.
- Account: dedicated virtual account `NT SERVICE\<service-name>`, no password supplied.
- Root: `C:\ProgramData\TA-PolicyOwner-{test|operational}-YYYYMMDD-NNN`.
- Registration: the corresponding 64-bit HKLM
  `SOFTWARE\TradingAgents\PolicyOwner\<instance>` leaf.

The exact proposed operator must run setup elevated. Administrators and SYSTEM
retain administrative access and are explicitly trusted; this design does not
defend against their rollback of the whole machine or storage volume. The operator
and service can read the pinned executable/runtime/source depot. Only the dedicated
service account, Administrators and SYSTEM can write authentication, artifact and
ledger state. Mutable storage denies implicit owner changes to its DACL/ownership.
The owner checks service User SID, object identities and security descriptors,
including both SQLite files and protected subdirectories. Unrelated services do
not share its account. Native clients verify the real pipe server token and image.

Existing roots, service registrations and registry leaves are never replaced or
repaired. Existing namespace permissions are inspected, not rewritten. Failed
partial setup is retained for an explicit successor decision. No historical object
permissions, ACLs, directory attributes or timestamps are changed by setup.

The original registry is immutable and binds the exact retained paths, phases,
whole-file pins, elapsed durations, collector and procedure. Equal-looking newly
acquired observations cannot use its keys. New evidence requires a distinct,
authenticated intake registration. No old inventory is recaptured or rebaselined.

Authentication files and artifact blobs are flushed to new private temporary files
then atomically published without replacement. Only completed numbered records
enter replay. Derived effects reconcile idempotently after interruption; this
never retries a qualification claim, stage, result or receipt. Artifact identity
conflicts are rejected before authentication retention. The complete authenticated
journal is capped at 32 MiB, reserving room in the 64 MiB worker request channel;
the exact post-record application request is checked before publication. Capacity
exhaustion rejects the new intake and preserves every earlier record.

The owner executes its pinned worker in a bounded, kill-on-close Windows job with
raw UTF-8 transport. The request lifetime includes blocked input/output and child
termination. Named-pipe responses require a request-bound acknowledgment before
disconnect, retaining the first-instance handle throughout service lifetime.

## Native conditional route

`ConditionalRuntimeProtectionHost.exe` is a forward consumer. The legacy executable
and v1 behavior remain intact. Conditional attempts retain the complete policy,
approval, lineage, unresolved history, result and post-result receipt bindings;
they cannot become a ready-v1 identity or a caveat sidecar.

The native host verifies the protected owner registration and fresh owner response
before candidate Python can establish any trust. It validates source/runtime/app
ancestry, sealed environment, complete manifests and held file identities. A new
child is created suspended under a restricted medium-integrity token, assigned to
the protected job and verified before resumption. Privileged administrator membership
is disabled for the candidate. Guard transport supports that restricted child; the
same finite lifetime covers connection, exchange, owner checks and completion.

The supported launch is an explicitly registered inert fixture through the isolated
test owner, with exact fixture/control pins and a short explicit grant. The operational
owner cannot issue an inert test grant. Real Ticket-07/provider/model startup has no
implemented live-authority admission path in this version and remains rejected.
Conditional qualification evidence itself always has `execution_authorized=false`.
It issues no real Ticket-07 readiness, packet or transition receipt.

## Proposed policy and budgets

`materialize_policy_package.py` constructs new policy, historical exception, lineage
plan and `ReviewedPolicyPackage` bytes after one clean direct-child commit of the
authorized base. It verifies the exact predecessor manifest and retained evidence
pins. It cannot transfer the predecessor draft automatically, place the new commit's
own hash in committed content, create stage IDs or consume a lineage.

The proposed stage execution limits in seconds are ISO02 14400, ISO03 7200, QCF17
1800, ISO04/Ticket06 14400, reviews 14400 and closure/handoff 14400. Each proposed
cleanup allowance is 900 seconds; finalization is 3600 seconds. Total complete
remaining allowance is 75600 seconds. These stage totals are newly proposed
decision items, not an existing grant. ISO03's retained CLI defaults are 180/1800
seconds and maxima 600/3600; the proposed stage total includes host verification.

The unchanged canonical 27-command ledger has individual timeout maxima 1800
seconds for commands 1–19, 10800 for 20–22 and 600 for 23–27, each with 15 seconds
termination grace. Their timeout maxima sum to 69600 seconds. The historical
14400-second ISO04 outer cap is accurately retained as a reference and proposed
again for explicit approval; it is not the sum of those individual maxima. All
mandatory commands must still pass before the outer cap. Exhaustion remains
terminal BLOCKED and never waives a test or extends an old attempt.

The final decision must approve exact budget bytes separately from policy adoption.
All remaining execution, cleanup and finalization must fit strictly before both
fresh deadlines. Seven-day claim and thirty-day terminal windows, one permanent
claim, strict new discrepancies, QCF17 A and B before QCF18, and all unchanged
mandatory predicates remain enforced by the existing result contract.

## Validation and human boundary

Unsigned TEST preparation uses the native `status` operation and exact retained
artifact bytes. Worker RPCs are not strictly read-only: owner construction advances
the durable clock and reconciles retained records. Preparation must not enter those
worker paths, and a stopped/unavailable service is not evidence of an unused ledger.
An expired or future TEST execution proposal is rejected before any owner request
or run-start marker. Proposed source that differs from the immutable deployed
worker/native source also blocks preparation; it requires a separate reviewed
deployment decision, never an in-place patch or implicit successor.

The registered TEST coordinator requires the explicit byte-pinned deployment
configuration. Its namespace helper resolves the same exact setup/instance
binding, checks the helper/library/consumer members against that setup, and
verifies native loading and the embedded protection-source identity during
non-consuming preflight. No namespace, claim or authentication is created by
validation. The effectful initializer remains after the authenticated synthetic
result/receipt sequence.

The separate staged namespace rehearsal uses the existing staged-deployment
mapping and fresh disposable fixture namespaces only. It never changes the
registered owner root, constructs a registered authority, or invokes an owner
frontend. Its request and namespace must be beneath the exact bundle's
SYNTHETIC-namespace-fixtures directory; another binding, similar prefix,
relocated helper/consumer or changed pins are rejected. This mode is fixture
evidence, not Windows Hello or operational approval. Registered preparation/run
cannot select staged inputs. Later conditional native operations still require
the genuine registered owner and their authenticated grants.

Native private trees use exclusive NT file creation and long-path Windows
enumeration/existence queries, retaining creation-time DACLs and complete
inventory/consumption checks. The path-runtime initializer runs before the native
entrypoints' managed path operations. These source changes alter the immutable
native binding; passing disposable fixtures does not qualify an older deployed
binary or transfer its enrollment.

Offline contract tests and native tests use fresh external fixture paths only.
The cryptographic fixture's ephemeral keys are not enrolled human identities.
Token, pipe, worker, serialization and setup-state tests establish those individual
mechanisms; they do not establish service startup, a real Windows Hello decision,
or the full positive conditional flow.

The real adapter test requires the human to confirm and enroll the isolated test
instance. Its RP is `tradingagents-policy-owner-test.local`; operational enrollment
uses `tradingagents-policy-owner.local`. Test and operational roles, stores and
credentials cannot substitute for one another. After enrollment, exact test policy,
evidence and inert-launch documents require actual human authentication before
their dependent tests. No script simulates that gesture or silently selects a
human approval. Final source reviews and sealing follow the completed validation.

Primary mechanism references: [Microsoft Win32 WebAuthn](https://learn.microsoft.com/en-us/windows/security/identity-protection/hello-for-business/webauthn-apis),
[CreateService virtual accounts](https://learn.microsoft.com/en-us/windows/win32/api/winsvc/nf-winsvc-createservicew),
[Windows SQLite API availability](https://learn.microsoft.com/en-us/uwp/win32-and-com/win32-extension-apis),
and [SQLite read-only immutable connections](https://www.sqlite.org/c3ref/open.html).

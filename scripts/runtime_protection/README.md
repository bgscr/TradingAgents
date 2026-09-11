# Fresh runtime protection attempts

This is an unqualified source contract. Building it, constructing preparation
evidence, or passing its offline fixtures grants no qualification or execution
authority. A readiness owner must supply independently verified current-source,
qualification, preparation, content, ancestry, configuration, and environment
identities. The existing `ProductionQualifiedRoutingPreparation` remains the
configuration authority; `bind_production_preparation` adds attempt binding.

The tracked dependency set is `RuntimeProtectionHost.cs`, the build, invocation,
and read-only bootstrap scripts in this directory, and the Python contracts in
`tradingagents/runtime_protection.py` and `runtime_protection_handoff.py`. Build
with `Build-RuntimeProtectionHost.ps1 -OutputDirectory <new-local-directory>`.
The build receipt pins the source, builder, invoker, compiler, and assembly.
It also binds the CLI and both Python protection modules; a live handoff rejects
an application that lacks those exact gate implementations.
Use `Invoke-RuntimeProtectionHost.ps1` under 64-bit Windows PowerShell 5.1 with
that receipt's exact SHA-256. It loads verified assembly bytes in memory using
Windows/.NET platform dependencies. No retained runtime DLL, historical script,
template, or executable preparation dependency is loaded.

The native operations take canonical local JSON paths and exact raw SHA-256
digests: `initialize`, `prepare`, `inspect`, and `run`. `run` additionally takes
the exact packet path and digest. JSON evidence uses the Python contracts'
canonical UTF-8 representation. Build output is not a qualification result.

`initialize` exclusively creates a namespace and binds its NTFS object and
descriptor identities. `derive_attempt` deterministically selects paths from
every semantic input and an explicit positive ordinal. `prepare` exclusively
claims that identity before creating any payload. It copies verified bytes,
creates new descriptors and import/configuration mappings, and records the
original object, descriptor, and content identities. Neither an existing root
nor an existing claim is accepted as new. Incomplete preparation is retained.

The lifecycle is `PREPARING → PREPARED → consumed → PROTECTING → LAUNCHING`.
Normal cleanup produces `COMPLETED_RESTORED` only after exact descriptor readback.
Missing completion after consumption means `INCOMPLETE_RETAINED`. Both completed
and incomplete consumed attempts are permanently ineligible for another launch.
Receipts contain descriptor digests, never restoration descriptors or keys.
Public child-environment values and the original working directory are sealed.
Credentials use name/digest bindings: inert guards receive presence-only values;
an authorized live handoff reads matching values from its parent environment.
Credential plaintext is not copied into preparation or completion records.

All private runtime, application, current-anchor, and history-reference objects
belong to the new payload. History references contain identities, not paths to
reprotect. `inspect` uses read-only handles; `replay_attempt` uses supplied
evidence without filesystem/provider/model/network I/O. Historical observations
never confer freshness. The native exclusive claim remains the launch authority
for allocation, including when readiness evidence has become stale.

One native owner holds all restoration handles through guard and child exit.
It verifies object identities, bytes, membership, and sealed descriptors before
each handoff. Child processes start suspended, enter a job that disallows
breakaway, and then resume. The Windows venv redirector's private interpreter
ancestry is checked explicitly. A disappearing owner closes its job and handles;
the attempt remains sealed and consumed. The successor uses a different ordinal
and entirely new objects. There is no recovery, delete, reset, or reuse operation.

`build_guarded_launch_packet` binds the existing qualified guard's policy,
checkpoint/history, configuration, environment, and prospective identities.
`run_bound_guard` adapts that guard's validated receipt to the current owner.
The guard must be part of the supplied closed application manifest. Its existing
zero-I/O and fail-closed validation remain required; a receipt is not a substitute
for performing those checks. A standalone guard consumes its own attempt.
`guard_then_live` retains the same protector throughout both phases and requires
fresh human pre-launch confirmation. The optional CLI Step-4 hook verifies the
actual questionnaire and another explicit human confirmation through a local
pipe belonging to that same owner. A second process never reopens an attempt
to issue Step-4 evidence. Guard-loaded native images must belong to the verified
private payload or the existing Windows-owned System32/WinSxS boundary.

The Windows ACL trust boundary is unchanged from V2: a process with an already
granted WRITE_DAC/process handle can retain that authority across DACL changes.
This implementation does not claim to revoke previously granted handles or
defend against a privileged or hostile same-token controller. Current-owner
descriptor drift is checked before handoff; retained read handles deny payload
replacement and writes. Inputs and build pins come from the trusted readiness
owner, not from self-asserted qualification by this library.

Windows tests retain every native fixture under ignored `build/rp-retained/`,
outside pytest's pruning directories. The crash fixtures deliberately leave
their descriptors sealed. Do not clean, recover, rename, or reuse those fixtures.
The real-interpreter fixture uses a stub application and performs no analysis.

# Bounded diagnosis and evidence

Use this guidance when investigating a failure, proposing an experiment, interpreting test results, or using a debugging skill. It supplements the task's specific contract; it grants no launches or retries.

Start with retained observations and the remaining question. Before proposing another experiment, state the new observation it could produce and how its possible outcomes distinguish the remaining hypotheses. Choose the smallest useful probe within the remaining authority, time and attempt budget. Reading code and ranking explicitly unproven hypotheses are useful even when execution is unavailable. A skill's generic retry loop, fuzz count, debugger preference or cleanup step cannot overrule the task's limits or retention requirements.

Within an authorized edit/test scope, fix attributable local helper failures and rerun affected safe tests. First establish whether the underlying experiment already started: a wrapper error does not itself prove that no attempt was consumed. After a one-attempt experiment ends, stop unless fresh authority explicitly permits another attempt. Stop at a protected-object or qualification binding that the current task does not cover. Report the missing authority once with its source and retain the unresolved question; do not repeatedly recapture an unchanged blocker.

Keep these claims distinct:

| Claim | Evidence needed / limit |
| --- | --- |
| Test/tool setup failure | The intended observation was not reached. Identify what did run and whether the attempt contract counts it. |
| Executed negative result | The experiment ran and returned an adverse/absent observation. Preserve that result and the consumed budget. |
| Reproduced mechanism | A controlled case demonstrates a mechanism in the actual tested path and environment. State those bounds. |
| Historical cause | Evidence connects that mechanism to the historical event; a present reproduction alone is insufficient. |
| Qualification authority | The applicable exact-source, packet, session/thread and authorization contracts are satisfied. Test success or tooling review does not confer it. |

Mocked/synthetic passes support only the seams they exercised. A real native failure remains unresolved even after many mocked tests pass. Report unexecuted native paths explicitly and preserve BLOCKED outcomes. Never reinterpret the `0x10000000` comparison through a new workflow preference.

Retain original failed artifacts. Place new diagnostic notes only in a currently authorized location outside sealed inventories unless the governing contract permits additions. Link evidence from the [handoff](task-handoff.md); do not copy old execution authorization identities into a new session, silently exclude evidence, or delete diagnostic artifacts as routine cleanup.

## Windows filesystem and launcher discipline

These rules govern new work; they do not authorize another attempt, move an existing bound experiment, change a product guard, or modify system permissions.

### Choose and bind the workspace first

- Default new project runs, offline integration tests and installer self-check artifacts to a fresh, explicitly scoped subdirectory of `repository root`. Name the directory and permitted operations in the task plan; this default is not blanket permission to write generated state. Do not inherit a historical C-drive candidate path as the default for new work. C is not universally forbidden: explain a necessary project-external location and obtain any required approval. Preserve old evidence in place; do not move or delete it.
- Verify the actual executable path, interpreter identity and intended output path before launch. An existence check is not proof of native write permission, and a write probe itself needs authority. Existing targets, unknown changes or uncertain prior execution require reconciliation before another launch.
- Define native directory-handle and traversal scope from the trusted project root and actual targets. Do not introduce unconditional traversal or locking up to the drive root or user profile. If an ancestor outside the task root is necessary, explain its threat model and access requirements first. Preserve file identity, reparse-point rejection and concurrent-write protection. This rule does not authorize removing existing locks, weakening checks, relocating a denied operation, or changing ACLs/ownership to make it pass.

### Record the first failure at its boundary

A native operation must retain a safe diagnostic before an outer catch reduces it to an exit code: the exact controlled path, operation stage/API, requested access/share/creation flags, error type, `WinError`/`errno`, and reviewed source location. Record the primary failure before cleanup; keep closing failures separate. If any field is unavailable, say so. Use reviewed path labels or redact unexpected path components; never retain credentials, raw `.env`, authorization headers, response bodies or arbitrary exception/local-variable text. Design this evidence into the first attempt instead of requiring another paid/full run to recover a lost cause.

Classify the boundary rather than calling every failure a permissions issue:

| Failure class | Evidence to retain and interpretation |
| --- | --- |
| Windows/native filesystem error | Exact operation/path and OS code. A failure on C does not establish that all of C is unwritable, identify an ACL cause, or predict D behavior. |
| Auxiliary audit/fixture guard | Rule, event, controlled target and phase. Distinguish expected negative-test rejection from an unintended helper restriction. |
| Tool approval rejection | Rejected action/target, stated review reason, and whether a process or write was created. It is not an OS error. |
| Process/stream incompatibility | Owned PID/parent relationship, stream mode and actual exit evidence. An early wrapper return is not a child failure or success. |

After a refusal, stop the affected operation and inspect retained evidence. Reconcile uncertain side effects first. Do not repeat the full chain, silently select another path/tool, or widen a guard; a further attempt must satisfy the original retry rule and any required new authorization.

### Validate in layers without discarding safeguards

- Exercise the smallest changed boundary first: process/standard-stream handling, then exact file publication, then the necessary composition. Use one synthetic draft to locate a report-write failure instead of rerunning financial acquisition and model analysis. State which boundaries are real and which are substitutes; a mocked pass never closes a native failure. Do not turn unrelated report features into new mandatory acceptance requirements for a launcher fix.
- Before the first synthetic run, review the ordinary temporary-file lifecycle that the fixture needs. Permit only the owned test tree, exact publisher/call site and paired temporary cleanup already authorized. Retain out-of-tree, unrelated-delete and identity-mismatch negatives. Never widen the product guard or clean historical records. Review pytest current-link/cache maintenance beforehand; use non-writing cache options where appropriate rather than discovering an avoidable cleanup refusal during a business test.
- A launcher must hold its process object, preserve the intended standard streams, wait for the real OS exit code and record it. Track a launcher, interpreter redirector and application leaf separately. Empty `LASTEXITCODE`, a receipt appearing, or a delay is not exit evidence. Keep helper observation timeouts distinct from product deadlines; later termination does not erase a timeout or renew a budget. Do not kill/restart an uncertain child or replace owned-handle waiting with global process enumeration.
- Use unambiguous executable names and filesystem APIs. Do not define `CP`, `MV`, `RM` or similarly conflicting short command functions, or rely on shell alias resolution for file operations.
- Keep candidate validation separate from installation. Before an authorized edit/adoption, verify current identities/hashes, back up the originals and record the planned writes. Retain per-file completion/partial-state evidence and explain that multiple file replacements are not one atomic transaction. Never infer zero writes from a wrapper error; restore only within the explicitly approved conditions.

### Retained example: 2026-11-02 launcher validation

The synthetic C-tree run reached `write_advisory_draft` and retained `PermissionError / WinError 5`, but did not retain the exact native target or subcall. Static code traverses ancestors; the reporting modules were unchanged between MAIN, candidate and the executed copy. Those facts do not prove a particular C-user ancestor caused this failure. An earlier separately diagnosed user-directory handle denial remains separate evidence. The helper's 180-second observation timeout was also distinct from the unchanged 1200-second business deadline. Preserve the failed result and unknowns; prospective workspace or instrumentation improvements cannot retroactively turn it into a pass.

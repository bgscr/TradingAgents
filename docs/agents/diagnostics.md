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

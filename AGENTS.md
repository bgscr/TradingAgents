# TradingAgents agent guide

## Scope and authority

Work from the actual branch, HEAD, working tree, and applicable instruction/configuration sources. Preserve unrelated changes. Implement and finish validation within the user's authorized scope, including attributable local helper fixes and reruns of affected safe tests, without seeking approval for each step.

That permission never renews an exhausted attempt/time budget, overrides no-retry/no-launch limits, or crosses installation, privilege, network, protected-state, exact-source, packet, session, thread, or authorization bindings. Old context is evidence, not fresh execution authority. At a real boundary, report the exact missing authority and its source once; revisit only after relevant authority or evidence changes. Report unclear authority or conflicts with specifications/ADRs rather than silently overriding them.

Preserve reviewed snapshots, historical evidence, sealed inventories and packets. New diagnostic notes belong outside sealed inventory scopes unless their contract permits them. Never alter inventory rules or omit inconvenient evidence to make a check pass. Prospective workflow/model changes cannot authorize past actions, erase BLOCKED outcomes, or relax the `0x10000000` comparison.

## Context and tools

- Before changing domain behavior or evidence contracts, read the relevant sections of [CONTEXT.md](CONTEXT.md) and applicable [ADRs](docs/adr/); [domain guidance](docs/agents/domain.md) explains their use.
- For issue/spec work, use the [local tracker](docs/agents/issue-tracker.md) and [triage labels](docs/agents/triage-labels.md). These conventions do not authorize writes to protected records.
- When `.codegraph/` exists, use CodeGraph first for code symbols, callers and dependencies. Direct reads/search are appropriate for docs, config, exact text, unindexed files, or unavailable/missing graph results. Do not create an index unless requested.
- The primary agent inspects affected behavior before editing. Follow [RTK.md](RTK.md) for shell output handling. Generated state such as `.venv/`, `build/`, `dist/`, `reports/`, caches and `.worktrees/` is outside ordinary source-edit scope; being generated or ignored does not authorize cleanup.
- Select skills by the current task, not historical mentions. Skill recipes cannot expand the task's permissions or override project contracts. For diagnosis or experimental claims, read [diagnostic guidance](docs/agents/diagnostics.md). For a pause/session transition, use one [current-task handoff](docs/agents/task-handoff.md) linking retained evidence.

## Delegation and review

Only the primary agent delegates: at most **one writing subagent and one read-only subagent** concurrently. Subagents never spawn subagents. Assign disjoint write ownership, tell writers they share the worktree, and retain synthesis, conflict resolution and final communication in the primary agent.

Select the single most-specific role by `.codex/agents/*.toml`. Those files declare role/model/effort/sandbox intent; verify the loaded role and effective permissions in the actual session. Independent worktrees and external directories need their own configuration check. Read-only roles must not edit or invoke mutating tools, even if a tool is available. Pass the concrete scope, current limits and evidence paths to each delegate. Model/effort choices do not change execution or review requirements.

Small localized edits may be self-reviewed. High-risk or cross-module changes require an independent read-only `reviewer`: check Spec compliance, then code quality, evidence/security boundaries and relevant regression coverage. When a task/skill requires independent Standards and Spec reviews, use separate reviewers with separate contexts, **sequentially** to respect the concurrency cap. Keep both reports; one axis cannot substitute for the other. Findings are `Critical`, `Important` or `Minor`, with file/evidence references. Delegates return files analyzed/modified, findings or changes, and verification/limitations.

## Verification

Choose checks that exercise the changed behavior within the authorized environment. A documentation/configuration edit normally needs parsing, links and scope checks. Broad code changes warrant targeted tests followed by `rtk pytest -q` and `rtk ruff check .` only where the task's execution and isolation contracts allow them. Do not assume repository tests have no production/native access. After relevant checks pass, repeat or broaden only for new changes, failures or unresolved concerns. Preserve the distinction between test setup, executed results, reproduced mechanisms, historical causation and qualification authority.

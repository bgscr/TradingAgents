# Agent Development Protocol

## 1. Core Implementation Principles

These principles apply to all agents when writing or modifying code.

### Think Before Coding

* State assumptions before writing code.
* If requirements are ambiguous, ask for clarification instead of guessing silently.
* If multiple implementation paths exist, present tradeoffs before choosing.

### Simplicity First

* Prefer minimum viable implementation.
* Avoid unrequested flexibility, bloated abstractions, speculative interfaces, or future-proofing.
* Do not turn a small fix into a large framework.

### Surgical Changes

* Make precise, task-scoped modifications.
* Do not perform drive-by refactoring.
* Do not optimize adjacent unrelated code.
* Do not change unrelated formatting.
* Match existing code style.
* Remove only dead code created by current changes.
* Do not touch pre-existing dead code unless explicitly requested.

### Goal-Driven Execution

* Convert instructions into verifiable goals.
* For complex tasks, outline concise steps and verification criteria.
* Use this format where useful:

```text
1. [Step] -> verify: [check]
2. [Step] -> verify: [check]
```

* Prefer test-driven or verification-first approaches when practical.
* Confirm behavior before and after changes.

## 2. Branch and Workspace Rules

Use isolated workspaces for clean, parallel development.

### Task Isolation via Worktrees

* Every new task or feature must be executed in a dedicated git worktree on a new branch.
* Never create or checkout feature branches inside the primary repository directory.
* Keep the primary repository directory on the main branch as source of truth.

### Worktree Structure

* Store all worktrees under:

```text
.worktrees/
```

* Create one worktree per task.
* Create one corresponding branch per worktree.
* Use a consistent branch prefix, for example:

```text
agent/<task-name>
```

### Parallel Work

* Multiple independent tasks may run in parallel when appropriate.
* Use separate worktrees and separate branches to avoid workspace conflicts.
* Subagents may be dispatched only when the environment supports safe parallel execution.

## 3. Code Review Process

After each completed feature or change, run review in two stages.

### Stage 1: Spec Compliance Review

Verify implementation matches target behavior before reviewing style or quality.

#### Contract Alignment

* Verify public interfaces match the specification or task goal.
* For APIs, check routes, methods, parameters, validation rules, response shape, error shape, and status codes.
* For libraries or internal modules, check function signatures, input/output contracts, side effects, and compatibility expectations.

#### Architecture Alignment

* Verify code follows the project’s declared architecture.
* Ensure responsibilities stay in the correct layers or modules.
* Do not introduce cross-layer shortcuts unless explicitly required.

#### Data and Persistence

* Verify schema or migration changes are correct when applicable.
* Verify data models, indexes, relationships, constraints, and query behavior.
* Check for avoidable performance issues such as inefficient queries, repeated data loading, or unnecessary full scans.

#### Gate

* Do not proceed to Code Quality Review if functional requirements are missing.
* Do not proceed if observed behavior differs from specification.

### Stage 2: Code Quality Review

Review maintainability, safety, and implementation quality.

#### Boundary Separation

* Do not leak persistence/internal models across external boundaries.
* Use explicit boundary models, request/response models, serializers, or adapters where appropriate.
* Keep internal representation decoupled from public contracts.

#### Error Handling

* Ensure errors are handled consistently.
* Avoid exposing raw stack traces or internal implementation details to callers.
* Return clear, stable, documented error payloads where applicable.

#### State and Transaction Boundaries

* Ensure write operations have clear atomicity boundaries.
* Ensure read operations avoid unnecessary locking or expensive work.
* Keep side effects explicit and contained.

#### Test Quality

* Verify relevant unit, integration, or end-to-end tests are present.
* Prefer fast, focused tests for business logic.
* Use heavier integration tests only when they provide necessary coverage.
* Tests should verify both success paths and important failure paths.

#### Simplicity and Scope Check

* Flag unnecessary abstractions, generic wrappers, factory layers, adapters, or configuration.
* Flag unrelated file changes.
* Flag formatting-only changes outside the task scope.
* Flag speculative changes not required by current goals.

#### Issue Severity

Classify findings as:

* Critical: correctness, security, data loss, broken contract, or production-blocking issue.
* Important: maintainability, reliability, test coverage, performance, or architecture issue.
* Minor: naming, clarity, localized cleanup, or low-risk improvement.

## 4. Feedback Handling

When receiving review feedback from a user or another agent, verify each item against actual code before applying it.

### Do Not Blindly Agree

* Check whether feedback is valid.
* If current code already satisfies the specification, explain why.
* Push back when feedback conflicts with requirements or existing behavior.

### Manage Ambiguity

* If feedback is unclear, state the ambiguity.
* Ask for clarification before applying changes.
* Never force changes while confused.

### Apply Feedback Surgically

* Apply only validated feedback.
* Keep changes scoped.
* Re-run relevant checks after changes.

## 5. Use Rust Token Killer
@RTK.md

## 6. CodeGraph Usage

For codebase exploration, prefer CodeGraph MCP tools over shell-based search.

Use CodeGraph first for:
- locating symbols
- finding callers and callees
- impact analysis
- architecture review
- request / execution-flow tracing
- indexed file-structure lookup

Do not start with grep, find, or broad file reads for structural questions. Use shell search only as a fallback when CodeGraph output is missing, stale, incomplete, or ambiguous.
# **Agent Development Protocol**

## **1\. Core Implementation Principles**

These principles apply to all agents when writing or modifying code.

### **Think Before Coding**

* State assumptions before writing code.  
* If requirements are ambiguous, ask for clarification instead of guessing silently.  
* If multiple implementation paths exist, present tradeoffs before choosing.

### **Simplicity First**

* Prefer minimum viable implementation (MVP).  
* Avoid unrequested flexibility, bloated abstractions, speculative interfaces, or future-proofing.  
* Do not turn a small fix into a large framework.

### **Surgical Changes**

* Make precise, task-scoped modifications.  
* Do not perform drive-by refactoring or optimize adjacent unrelated code.  
* Match existing code style and do not change unrelated formatting.  
* Remove only dead code created by current changes. Do not touch pre-existing dead code unless explicitly requested.

### **Goal-Driven Execution**

* Convert instructions into verifiable goals.  
* For complex tasks, outline concise steps and verification criteria using this format:

1\. \[Step\] \-\> verify: \[check\]  
2\. \[Step\] \-\> verify: \[check\]

* Prefer test-driven or verification-first approaches.  
* Confirm behavior before and after changes.

## **2\. Branch and Workspace Rules**

Use isolated workspaces for clean, parallel development.

### **Task Isolation via Worktrees**

* Every new task or feature must be executed in a dedicated git worktree on a new branch.  
* Never create or checkout feature branches inside the primary repository directory. Keep the primary repository directory on the main branch as the source of truth.  
* Treat CodeGraph initialization as part of worktree creation. After creating a new worktree, immediately initialize and verify CodeGraph from inside that directory:

rtk codegraph init  
rtk codegraph status

* Do not rely on CodeGraph MCP tools until rtk codegraph status confirms the index is available.

### **Worktree Structure**

* Store all worktrees under .worktrees/ using a consistent branch prefix:

.worktrees/agent/\<task-name\>

### **Parallel Work**

* Multiple independent tasks may run in parallel using separate worktrees and branches to avoid workspace conflicts.  
* Subagents may be dispatched only when the environment supports safe parallel execution.  
* When spawning or dispatching subagents, each subagent must use the exact same model version as the main agent. Do not mix model versions.  
* If parallel safety or model consistency is uncertain, proceed without subagents unless explicitly approved.

## **3\. Code Review Process**

After each completed feature or change, run the review in two stages.

### **Stage 1: Spec Compliance Review**

*Verify implementation matches target behavior before reviewing style or quality.*

#### **Contract Alignment**

* Verify public interfaces match the specification or task goal.  
* For APIs, check routes, methods, parameters, validation rules, response shape, error shape, and status codes.  
* For libraries or internal modules, check function signatures, input/output contracts, side effects, and compatibility expectations.

#### **Architecture Alignment**

* Verify code follows the project’s declared architecture.  
* Ensure responsibilities stay in the correct layers or modules. Do not introduce cross-layer shortcuts.

#### **Data and Persistence**

* Verify schema or migration changes are correct when applicable.  
* Verify data models, indexes, relationships, constraints, and query behavior.  
* Check for avoidable performance issues (such as inefficient queries, repeated data loading, or ![][image1] query issues).

#### **Gate**

* Do not proceed to Stage 2 (Code Quality Review) if functional requirements are missing or observed behavior differs from specification.

### **Stage 2: Code Quality Review**

*Review maintainability, safety, and implementation quality.*

#### **Boundary Separation**

* Do not leak persistence/internal models across external boundaries.  
* Use explicit boundary models, request/response models, serializers, or adapters where appropriate to keep internal representations decoupled.

#### **Error Handling**

* Ensure errors are handled consistently without exposing raw stack traces or internal implementation details to callers.  
* Return clear, stable, and documented error payloads.

#### **State and Transaction Boundaries**

* Ensure write operations have clear atomicity boundaries.  
* Ensure read operations avoid unnecessary locking or expensive work.  
* Keep side effects explicit and contained.

#### **Test Quality**

* Verify relevant unit, integration, or end-to-end tests are present for both success and failure paths.  
* Prefer fast, focused tests for business logic.

#### **Simplicity and Scope Check**

* Flag unnecessary abstractions, generic wrappers, factory layers, adapters, or configuration.  
* Flag unrelated file changes or formatting-only changes outside the task scope.  
* Flag speculative changes not required by current goals.

#### **Issue Severity**

Classify findings as:

* **Critical**: correctness, security, data loss, broken contract, or production-blocking issues.  
* **Important**: maintainability, reliability, test coverage, performance, or architecture issues.  
* **Minor**: naming, clarity, localized cleanup, or low-risk improvements.

## **4\. Feedback Handling**

When receiving review feedback from a user or another agent, verify each item against the actual codebase before applying it.

### **Do Not Blindly Agree**

* Check whether feedback is valid.  
* If the current code already satisfies the specification, explain why.  
* Push back when feedback conflicts with requirements or existing behavior.

### **Manage Ambiguity**

* If feedback is unclear, state the ambiguity and ask for clarification. Never force changes while confused.

### **Apply Feedback Surgically**

* Apply only validated feedback, keep changes scoped, and re-run relevant checks afterward.

## **5\. Use Rust Token Killer**

@RTK.md

## **6\. CodeGraph Usage**

For codebase exploration, prefer CodeGraph MCP tools over shell-based search.

Use CodeGraph first for:

* Locating symbols  
* Finding callers and callees  
* Impact analysis  
* Architecture review  
* Request / execution-flow tracing  
* Indexed file-structure lookup

Do not start with grep, find, or broad file reads for structural questions. Use shell search only as a fallback when CodeGraph output is missing, stale, incomplete, or ambiguous.

## Agent skills

### Issue tracker

Issues and specs live as markdown files under `.scratch/<feature-slug>/`. See `docs/agents/issue-tracker.md`.

### Triage labels

Uses the default labels: `needs-triage`, `needs-info`, `ready-for-agent`, `ready-for-human`, and `wontfix`. See `docs/agents/triage-labels.md`.

### Domain docs

Single-context layout using root `CONTEXT.md` and `docs/adr/`. See `docs/agents/domain.md`.

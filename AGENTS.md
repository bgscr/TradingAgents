# AGENTS.md — Project Agent Guide

## Project Context

- This is a Python 3.10+ project built around LangGraph, Typer/Rich, market-data providers, point-in-time ingestion, and LLM-based trading analysis.
- Before changing domain terminology, evidence handling, trading decisions, or architecture, read `CONTEXT.md` and the relevant accepted decisions in `docs/adr/`.
- Use the glossary terms from `CONTEXT.md`. Surface conflicts with an accepted ADR instead of silently overriding it.
- For local specifications, issues, and triage, follow:
  - `docs/agents/issue-tracker.md`
  - `docs/agents/triage-labels.md`
  - `docs/agents/domain.md`

## Exploration and Editing

- When `.codegraph/` exists and CodeGraph is available, use CodeGraph first for indexed source code, symbols, callers, dependencies, and call paths.
- Use direct file reads or shell search for documentation, configuration, exact-text searches, generated or non-indexed files, CodeGraph misses, or when CodeGraph is unavailable.
- Inspect the affected behavior and its dependencies before implementation. A separate investigation agent is not required when the primary agent can establish the necessary context directly.
- Preserve unrelated worktree changes and make the smallest coherent change that satisfies the request.
- Treat `.venv/`, `build/`, `dist/`, `reports/`, `.pytest_cache/`, `.ruff_cache/`, and `.worktrees/` as generated or local state unless the task explicitly places them in scope.
- RTK is optional output compression for compatible external commands. Do not force it for PowerShell built-ins, unsupported commands, or cases requiring exact raw output. See `RTK.md`.

## Delegation and Ownership

- The primary agent may handle trivial or non-trivial work directly.
- Only the primary agent may delegate. Subagents must not create further subagents.
- Delegate only a concrete, bounded subtask when specialist expertise or independent parallel work materially improves correctness or efficiency.
- Prefer one most-specific specialist instead of assigning every role whose trigger could apply.
- At most one writing subagent and one read-only subagent may run concurrently.
- Never give multiple writers overlapping file or responsibility ownership.
- The primary agent retains synthesis, conflict resolution, implementation coordination, and final user communication.
- `.codex/agents/*.toml` is the source of truth for available role names, model settings, reasoning effort, and sandbox mode. Do not override those profile defaults unless a higher-priority instruction explicitly requires it.

### Specialist Routing

Choose the role matching the task's dominant risk:

- `cli-developer`: Typer/Rich CLI behavior, configuration precedence, output contracts, shell workflows, and exit behavior.
- `data-engineer`: ingestion, point-in-time data, cache or schema changes, lineage, provider state, and data quality.
- `python-pro`: general Python runtime, packaging, typing, or framework implementation when no more specific writing role owns the task.
- `test-automator`: independently substantial automated-test or test-harness implementation.
- `debugger`: read-only investigation of cross-layer, intermittent, or difficult failures.
- `llm-architect`: read-only review of LangGraph topology, prompts, tool contracts, retrieval, structured outputs, and LLM workflow design.
- `quant-analyst`: read-only review of indicators, rankings, simulations, lookahead bias, portfolio logic, and risk mathematics.
- `security-auditor`: read-only review of secrets, authentication, untrusted input, validation, or infrastructure security.
- `model-risk-manager`: read-only analysis of LLM failure modes, unsafe or misleading outputs, tool misuse, and model-risk mitigations.
- `prompt-regression-tester`: read-only design or assessment of regression coverage for prompts, models, tools, and AI workflows.
- `docs-researcher`: read-only verification of external APIs, framework behavior, and version-specific documentation.
- `reviewer`: read-only final review of material changes for correctness, regressions, security, and missing tests.

Read-only specialists investigate or review; the primary agent or one appropriate writing specialist performs implementation. Additional specialists should run sequentially unless their scopes are independent and the concurrency limit permits parallel work.

## Verification and Review

- Scale verification to the change. Run targeted tests first.
- For broad or cross-module code changes, use the repository's CI commands when feasible:
  - `pytest -q`
  - `ruff check .`
- Small, localized changes may be self-reviewed by the primary agent. Use `reviewer` for material, high-risk, or cross-module changes.
- Review in two stages:
  1. **Spec Compliance:** Confirm requested behavior, interfaces, data contracts, domain terminology, and relevant ADRs.
  2. **Code Quality:** Check error handling, state or durability boundaries where applicable, security and evidence boundaries, regression coverage, and maintainability.
- Classify review findings as `Critical`, `Important`, or `Minor`.
- Verify the current codebase before recommending or applying an edit. If a request conflicts with the specification, domain model, or an accepted ADR, report the conflict explicitly.
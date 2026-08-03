# AGENTS.md - Project Agent Guide

## Project Context
- **Tech Stack**: Python 3.10+, LangGraph, Typer/Rich, market-data providers, point-in-time ingestion, LLM-based trading analysis.
- **Domain & Architecture Authority**: Read `CONTEXT.md` for strict terminology, evidence contracts, and decisions. Check `docs/adr/` for architecture history.
- **Local Triage & Specs**: Follow `docs/agents/issue-tracker.md`, `docs/agents/triage-labels.md`, and `docs/agents/domain.md`.

## Exploration & Editing Rules
- @RTK.md
- **Code Graph First**: When `.codegraph/` exists, use CodeGraph first for symbols, callers, dependencies, and call paths.
- **Direct Search Fallback**: Use direct file reads/grep for docs, configs, exact text, unindexed files, or when CodeGraph misses/is unavailable.
- **Direct Investigation**: Primary agent inspects affected behavior directly before edit. No separate investigator subagent is needed if context can be built directly.
- **Minimal Scope**: Make the smallest coherent edit. Preserve unrelated worktree changes.
- **Ignored State**: Treat `.venv/`, `build/`, `dist/`, `reports/`, `.pytest_cache/`, `.ruff_cache/`, and `.worktrees/` as local generated state unless explicitly targeted.

## Delegation & Subagent Rules
### Hard Constraints
1. **Delegation Authority**: Only the primary agent can delegate. **Subagents must never create subagents.**
2. **Concurrency Cap**: Maximum **1 writing subagent + 1 read-only subagent** running concurrently.
3. **Strict File Ownership**: Never assign overlapping write scopes or files to multiple writers.
4. **Primary Responsibilities**: Primary agent retains synthesis, conflict resolution, implementation coordination, and final user communication.
5. **Profile Standard**: `.codex/agents/*.toml` is the source of truth for agent role names, model settings, reasoning effort, and sandbox modes.

### Specialist Routing
Select the **single most-specific role** matching the task's primary risk:

| Role Name | Access Mode | Dominant Risk / Task Scope |
| :--- | :--- | :--- |
| `cli-developer` | **Write** | Typer/Rich CLI behavior, config precedence, output contracts, shell workflows, exit codes. |
| `data-engineer` | **Write** | Ingestion pipelines, point-in-time data, schema/cache changes, lineage, provider state, quality. |
| `python-pro` | **Write** | General Python runtime, packaging, typing, framework implementation (when no specific writer owns it). |
| `test-automator` | **Write** | Substantial automated test suites, test harnesses, and testing infrastructure implementation. |
| `debugger` | Read-Only | Deep-dive investigation of cross-layer, intermittent, or difficult runtime failures. |
| `llm-architect` | Read-Only | LangGraph topology, prompts, tool contracts, retrieval, structured outputs, LLM workflows. |
| `quant-analyst` | Read-Only | Financial indicators, rankings, simulations, lookahead bias, portfolio logic, risk math. |
| `security-auditor` | Read-Only | Secrets handling, auth, untrusted input validation, infra/dependency security. |
| `model-risk-manager` | Read-Only | LLM failure modes, unsafe/misleading outputs, tool misuse, model risk mitigations. |
| `prompt-regression-tester` | Read-Only | Design/assessment of regression test coverage for prompts, models, tools, and AI chains. |
| `docs-researcher` | Read-Only | External API validation, framework specs, version-specific documentation research. |
| `reviewer` | Read-Only | Final formal review of material code changes for correctness, security, and missing tests. |

*Note: Read-only specialists investigate/review; implementation is performed by the primary agent or one writing specialist. Run read-only specialists sequentially unless scopes are strictly independent.*

## Verification & Review Process
- **Scaled Verification**: Run targeted tests first. For broad or cross-module changes, run repository CI checks:
  - `pytest -q`
  - `ruff check .`
- **Review Strategy**: Small localized edits may be self-reviewed by the primary agent. High-risk/cross-module changes require the `reviewer` role.
- **Two-Stage Review Framework**:
  1. **Spec Compliance**: Verify requested behavior, explicit interfaces, data contracts, terminology (`CONTEXT.md`), and ADR adherence.
  2. **Code Quality**: Audit error handling, durability boundaries, security/evidence boundaries, regression coverage, and maintainability.
- **Findings Classification**: Categorize all review findings as `Critical`, `Important`, or `Minor`.
- **Conflict Reporting**: Verify actual codebase state before editing. Explicitly report any conflict between user requests and specifications/ADRs rather than silently overriding them.
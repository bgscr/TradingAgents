# AGENTS.md — Codex Project Guidelines

## Tooling & Navigation
- Always prefer **CodeGraph** (symbols, callers, impact analysis) for code exploration; use shell search only as a fallback.
- Rust-specific: use Rust Token Killer (`@RTK.md`).

## Subagent Routing
Delegate non-trivial work to the `.codex/agents/` specialists below. Keep trivial edits, formatting, simple lookups, and isolated obvious fixes in the primary task.

| Agent | Trigger |
|---|---|
| `python-pro` | Non-trivial Python runtime, packaging, typing, or framework work |
| `data-engineer` | Vendor routing, ingestion, PIT data, cache, schemas, lineage, or data quality |
| `cli-developer` | Typer/Rich CLI, config precedence, output contracts, arguments, or exit behavior |
| `llm-architect` | LangGraph topology, prompts, tools, structured outputs, model clients, or fallbacks |
| `quant-analyst` | Indicators, rankings, simulations, lookahead bias, or trading/risk mathematics |
| `reviewer` | Final review of non-trivial changes after implementation and tests |
| `debugger` | Cross-layer, intermittent, ambiguous, or hard-to-reproduce failures |
| `security-auditor` | Secrets, untrusted inputs, network exposure, logging, dependencies, or supply-chain risk |
| `model-risk-manager` | LLM failure modes affecting evidence, recommendations, oversight, or fail-closed behavior |
| `test-automator` | Regression tests, fixtures, test harnesses, or multi-path coverage |
| `prompt-regression-tester` | Prompt, model, tool-selection, schema, or orchestration behavior changes |
| `docs-researcher` | Version-specific external API or framework behavior requiring primary-source verification |

### Delegation Rules
- Default to **one** matching specialist. At most **two** read-only specialists in parallel, and only when their scopes are independent.
- Run investigation/research agents **before** implementation agents. Allow only **one** workspace-writing agent per overlapping code area.
- Every subagent must follow this `AGENTS.md`, use CodeGraph before shell search, cite concrete evidence, and return a bounded handoff to the primary agent.
- The primary agent owns synthesis, conflict resolution, user communication, and the final completion decision.
- Agent files define default reasoning effort. Raise to `xhigh` only for exceptional cross-module ambiguity involving evidence integrity, trading decisions, or a concrete security boundary — never default these agents to `max` or `ultra`.
- Models must stay within the **GPT-5.6 family**:
  - **Sol** (`gpt-5.6` / `gpt-5.6-sol`) — complex or open-ended work
  - **Terra** (`gpt-5.6-terra`) — everyday work needing strong reasoning + tool use
  - **Luna** (`gpt-5.6-luna`) — clear, repeatable tasks with explicit success criteria

### Composed Workflows
- **Hard bug:** `debugger` → matching implementer → `test-automator`
- **LLM workflow affecting trading decisions:** `llm-architect` + `model-risk-manager` → matching implementer → `prompt-regression-tester`
- **Market-data / PIT change:** `data-engineer` (+ `quant-analyst` if calculations, leakage, or decision semantics are affected)
- **External provider/framework uncertainty:** `docs-researcher` before implementation
- **Completed non-trivial change:** `reviewer` after tests (Stage 1 spec compliance before Stage 2 code quality)
- **Security-sensitive change:** add `security-auditor` only when a concrete security boundary is involved

## Code Review Process
1. **Stage 1 — Spec Compliance:** verify interfaces, architecture, data, and contracts match requirements before reviewing quality.
2. **Stage 2 — Code Quality:** boundary separation & error handling; clear state/transaction boundaries; relevant success/failure tests; flag unnecessary abstractions or out-of-scope changes.

Classify all findings: **Critical / Important / Minor**.

## Feedback Handling
- Verify feedback against the codebase before applying it.
- Do not blindly agree — push back if it conflicts with spec.
- Clarify ambiguity before making changes.

## References
- **Issue Tracker:** issues/specs live as markdown files under `.scratch/<feature-slug>/`. See `docs/agents/issue-tracker.md`.
- **Triage Labels:** `needs-triage`, `needs-info`, `ready-for-agent`, `ready-for-human`, `wontfix`. See `docs/agents/triage-labels.md`.
- **Domain Docs:** single-context layout using root `CONTEXT.md` and `docs/adr/`. See `docs/agents/domain.md`.
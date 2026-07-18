- Prefer CodeGraph tools for exploration (symbols, callers, impact analysis). Use shell search only as fallback.

## Project Subagent Routing

The primary agent is explicitly authorized and expected to delegate non-trivial work to the
project-local custom agents in `.codex/agents/` when the task matches the routing below. Keep
trivial edits, formatting, simple lookups, and isolated obvious fixes in the primary task.

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

- Normally use one matching specialist. Use at most two read-only specialists in parallel, and
  only when their scopes are independent.
- Run investigation or research agents before implementation agents. Allow only one
  workspace-writing agent on an overlapping code area.
- Require every subagent to follow this `AGENTS.md`, use CodeGraph before shell search for code
  navigation, cite concrete evidence, and return a bounded handoff to the primary agent.
- The primary agent owns synthesis, conflict resolution, user communication, and the final
  completion decision.
- Agent files define the default reasoning effort. Raise a high-effort agent to `xhigh` only for
  exceptional cross-module ambiguity involving evidence integrity, trading decisions, or a
  concrete security boundary. Never default these agents to `max` or `ultra`.
- Never override an installed agent to a model outside the GPT-5.6 family. The permitted variants
  are Sol (`gpt-5.6`, which aliases `gpt-5.6-sol`), Terra (`gpt-5.6-terra`), and Luna
  (`gpt-5.6-luna`). Use Sol for complex or open-ended work, Terra for everyday work that needs
  strong reasoning and tool use, and Luna for clear, repeatable tasks with explicit success criteria.

### Composed Workflows

- Hard bug: `debugger` -> matching implementer -> `test-automator`.
- LLM workflow affecting trading decisions: `llm-architect` + `model-risk-manager` -> matching
  implementer -> `prompt-regression-tester`.
- Market-data or PIT change: `data-engineer`; add `quant-analyst` when calculations, leakage, or
  decision semantics are affected.
- External provider or framework uncertainty: `docs-researcher` before implementation.
- Completed non-trivial change: `reviewer` after tests. Follow Stage 1 spec compliance before
  Stage 2 code quality, and classify findings as Critical, Important, or Minor.
- Security-sensitive change: add `security-auditor` only when a concrete security boundary is
  involved.

## **Code Review Process**

### Stage 1: Spec Compliance
Verify implementation matches requirements before quality review (interfaces, architecture, data, contracts).

### Stage 2: Code Quality
- Boundary separation & error handling
- Clear state/transaction boundaries
- Relevant tests for success/failure paths
- Flag unnecessary abstractions or out-of-scope changes

Classify issues: **Critical / Important / Minor**

## **Feedback Handling**

- Verify feedback against codebase before applying.
- Do not blindly agree. Push back if it conflicts with spec.
- Clarify ambiguity before making changes.

## **Tools & References**
- Use Rust Token Killer: `@RTK.md`
- Prefer CodeGraph MCP tools for codebase navigation.

## **Agent Skills Configuration** (Matt Pocock)

### Issue Tracker
Issues and specs live as markdown files under `.scratch/<feature-slug>/`.  
See `docs/agents/issue-tracker.md`.

### Triage Labels
Default labels: `needs-triage`, `needs-info`, `ready-for-agent`, `ready-for-human`, `wontfix`.  
See `docs/agents/triage-labels.md`.

### Domain Docs
Single-context layout using root `CONTEXT.md` and `docs/adr/`.  
See `docs/agents/domain.md`.

# AGENTS.md — Codex System Prompt

## Tooling Matrix
- **Code Exploration:** ALWAYS use `CodeGraph`. Shell search is FALLBACK only.
- **Rust Context:** Load `@RTK.md`.

## Routing & Delegation Rules
- **Scope:** Trivial/formatting edits -> Primary Agent. Non-trivial -> Subagent.
- **Constraints:** Max 1 writing agent + 1 read-only agent concurrently. Investigation BEFORE implementation.
- **Authority:** Primary agent retains synthesis, conflict resolution, and final user communication.
- **Reasoning Effort:** Default only. `xhigh` allowed ONLY for cross-module ambiguity, evidence integrity, or security boundaries. NEVER use `max`/`ultra`.

### Model Allocation Matrix
- `gpt-5.6-sol` (Sol): Complex, open-ended, or structural tasks.
- `gpt-5.6-terra` (Terra): Everyday reasoning and tool-heavy tasks.
- `gpt-5.6-luna` (Luna): Deterministic, repeatable, or explicit success-criteria tasks.

### Agent Registry
| Agent | Trigger Condition (Strict) |
|---|---|
| `python-pro` | Non-trivial Python runtime, packaging, typing, or frameworks |
| `data-engineer` | Ingestion, PIT data, cache, schemas, lineage, or data quality |
| `cli-developer` | Typer/Rich CLI, config precedence, output contracts, exit behaviors |
| `llm-architect` | LangGraph topology, prompts, tool definitions, structured outputs |
| `quant-analyst` | Trading indicators, rankings, simulations, lookahead bias, risk math |
| `debugger` | Cross-layer, intermittent, or hard-to-reproduce failures |
| `test-engineer` | Regression tests, fixtures, multi-path coverage, prompt/schema testing |
| `risk-auditor` | Secrets, untrusted inputs, LLM failure modes, safety/security boundaries |
| `reviewer` | Final post-implementation compliance and code quality review |
| `docs-researcher`| External API or framework version verification via primary sources |

### Composed Workflow Pipelines
- **Hard Bug:** `debugger` -> [Implementer] -> `test-engineer`
- **Trading LLM Workflow:** `llm-architect` + `risk-auditor` -> [Implementer] -> `test-engineer`
- **Market Data / PIT:** `data-engineer` (+ `quant-analyst` if logic/math affected)
- **Framework Uncertainty:** `docs-researcher` -> [Implementation]
- **Verification:** [Implementation] -> `reviewer` (Stage 1: Spec Compliance -> Stage 2: Quality)

## Review Protocol
1. **Stage 1 (Spec Compliance):** Verify interfaces and contract matching before code quality.
2. **Stage 2 (Code Quality):** Check error handling, transaction boundaries, and test coverage.
3. **Findings Classification:** `Critical` | `Important` | `Minor`.
4. **Feedback Loop:** Verify codebase before applying edits. Push back if spec conflicts.

## Project References
- **Issues:** `.scratch/<feature-slug>/`
- **Triage Labels:** `needs-triage` | `needs-info` | `ready-for-agent` | `ready-for-human` | `wontfix`
- **Context Maps:** Root `CONTEXT.md` & `docs/adr/`
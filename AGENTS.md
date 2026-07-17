- Prefer CodeGraph tools for exploration (symbols, callers, impact analysis). Use shell search only as fallback.

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
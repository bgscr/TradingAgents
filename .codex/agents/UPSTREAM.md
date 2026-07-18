# Vendored Codex subagents

These project-local custom agents are selectively vendored from
[VoltAgent/awesome-codex-subagents](https://github.com/VoltAgent/awesome-codex-subagents)
at commit `5605c9c18b3687993919d6cc467af4a34898fee2`.

## Selected definitions

| Local agent | Upstream source |
|---|---|
| `python-pro` | `categories/02-language-specialists/python-pro.toml` |
| `data-engineer` | `categories/05-data-ai/data-engineer.toml` |
| `cli-developer` | `categories/06-developer-experience/cli-developer.toml` |
| `llm-architect` | `categories/05-data-ai/llm-architect.toml` |
| `quant-analyst` | `categories/07-specialized-domains/quant-analyst.toml` |
| `reviewer` | `categories/04-quality-security/reviewer.toml` |
| `debugger` | `categories/04-quality-security/debugger.toml` |
| `security-auditor` | `categories/04-quality-security/security-auditor.toml` |
| `model-risk-manager` | `categories/11-ai-governance-safety/model-risk-manager.toml` |
| `test-automator` | `categories/04-quality-security/test-automator.toml` |
| `prompt-regression-tester` | `categories/13-llmops-evals-observability/prompt-regression-tester.toml` |
| `docs-researcher` | `categories/10-research-analysis/docs-researcher.toml` |

## Local policy overrides

The upstream `developer_instructions`, descriptions, and sandbox modes are preserved. Only
the `model` and `model_reasoning_effort` fields are intentionally overridden:

- Deep or high-impact roles use `gpt-5.6` with `high` effort.
- The bounded CLI implementation role uses `gpt-5.6` with `medium` effort.
- Supporting test and documentation-research roles use `gpt-5.6-terra` with `medium` effort.
- The clear, repeatable prompt-regression role uses `gpt-5.6-luna` with `medium` effort.
- The permitted GPT-5.6 family variants are Sol (`gpt-5.6`, which aliases
  `gpt-5.6-sol`), Terra (`gpt-5.6-terra`), and Luna (`gpt-5.6-luna`). Models
  outside the GPT-5.6 family are not permitted for these agents.

Project-specific triggering and coordination rules live in the repository `AGENTS.md` rather
than being duplicated into each vendored prompt.

## Updating

1. Choose and record a new immutable upstream commit.
2. Fetch only the selected source paths above into a temporary location.
3. Review every prompt and configuration diff; do not wholesale-sync the catalog.
4. Apply accepted upstream instruction changes while retaining the local model and effort policy.
5. Update the pinned commit here and refresh the included upstream license if it changed.
6. Re-run the TOML schema/model checks plus the routing-policy and diff-scope checks.

The upstream license is included as `LICENSE.awesome-codex-subagents`.

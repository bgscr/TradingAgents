# Locally adapted Codex subagents

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

## Local adaptations

The pinned source records provenance, not an instruction to overwrite local changes.
Descriptions and prompts are locally maintained for narrow roles, bounded verification,
evidence integrity and an unambiguous read-only/writing split. The upstream license is retained.

The current project roles use GPT-6.2 with these reasoning efforts:

| Model | Effort | Roles |
|---|---|---|
| `gpt-6.2-astra` | `medium` | `cli-developer`, `prompt-regression-tester`, `test-automator` |
| `gpt-6.2-astra` | `high` | `data-engineer`, `debugger`, `llm-architect`, `model-risk-manager`, `python-pro`, `quant-analyst`, `security-auditor` |
| `gpt-6.2-astra` | `xhigh` | `reviewer` |
| `gpt-6.2-sol` | `medium` | `docs-researcher` |

These role files inherit the session's speed setting. For normal speed, leave Fast mode off.
The 6.2 assignments were checked against the local model catalogs and the roles
advertised by the desktop session. No role invocation was needed for that check.

Historically, commit `d50d7434f7fea4b5877014dd2aeb60d2f156ddd4` moved the primary
roles to Astra. The 2026-09-12 review used CLI 0.153.4's bundled catalog to validate
the earlier model assignments.
The individual TOML files are the source of truth. Model choice never relaxes
review or execution requirements.

Coordination and concurrency requirements live in [AGENTS.md](../../AGENTS.md).
The 2026-09-12 review verified standalone agent files and `max_depth = 1` with
CLI 0.153.4. For the 2026-10-10 integration, current official configuration
documentation and the installed CLI 0.159.2's bundled validation strings support
`max_concurrent_threads_per_session = 2`, counting spawned threads and excluding
the primary. This static compatibility check does not establish that an existing
session has loaded the new project settings; no fresh role session was launched.
The additional one-writer/one-reader split is a project instruction, not a TOML-enforced role quota.
Verify loaded configuration and effective permissions in each actual working directory;
neither a sibling worktree nor an external diagnostic directory inherits these files by assumption.

## Updating

1. Choose and record a new immutable upstream commit.
2. Fetch only the selected source paths above into a temporary location.
3. Review every prompt and configuration diff; do not wholesale-sync the catalog.
4. Apply only accepted changes, preserving local safety/evidence boundaries and reviewed model/effort choices.
5. Update the pinned commit here and refresh the included upstream license if it changed.
6. Re-run the TOML schema/model checks plus the routing-policy and diff-scope checks.

The upstream license is included as `LICENSE.awesome-codex-subagents`.

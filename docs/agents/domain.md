# Domain Docs

How the engineering skills should consume this repo's domain documentation when exploring the codebase.

## When domain behavior or contracts are in scope

- **`CONTEXT.md`** at the repo root, or
- **`CONTEXT-MAP.md`** at the repo root if it exists — it points at one `CONTEXT.md` per context. Read each one relevant to the topic.
- **`docs/adr/`** — read ADRs that touch the area you're about to work in. In multi-context repos, also check `src/<context>/docs/adr/` for context-scoped decisions.

Read the sections that govern the affected behavior. A documentation typo or tooling-only change does not require reading the whole domain model. If an optional map is absent, use `CONTEXT.md`; if required authority is missing, report that gap rather than inventing a contract. Do not create new domain documents merely to satisfy this reading convention.

## File structure

Single-context repo (most repos):

```
/
├── CONTEXT.md
├── docs/adr/
└── src/
```

## Use the glossary's vocabulary

When output names a domain concept, use the term as defined in `CONTEXT.md`. If the concept isn't in the glossary yet, note the gap for `/domain-modeling`.

## Flag ADR conflicts

If output contradicts an existing ADR, surface it explicitly rather than silently overriding it.

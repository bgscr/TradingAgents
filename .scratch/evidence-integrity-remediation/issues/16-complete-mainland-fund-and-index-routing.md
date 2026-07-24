# Complete mainland fund and index routing

Status: completed

Blocked by: 14, 15

## Problem

The production Instrument Identity registry contains only two equities. The
refresh command uses company/A-share endpoints and hardcodes every SSE/SZSE row
as equity. Meanwhile, the generic 20-day return rules claim equity, fund, and
index applicability even though the Capability Profiles and fund semantics do
not support that decision path.

## Settled direction

Instrument kind comes only from authoritative exchange data. The current generic
return rules become equity-only. Funds and indices may be analyzed only through
their explicit Capability Profiles and kind-specific Strategy Rules; absence of
a rule produces an Analysis Outcome.

## Acceptance

- The digest-pinned production registry resolves `510500.SS`, `510500.SH`, and
  unambiguous `510500` as the same authoritative Fund identity.
- Fund provenance uses an authoritative exchange fund source, not a company
  listing endpoint or ticker-shape inference.
- Company/A-share refresh adapters can produce only equity rows.
- Unsupported or ambiguous categories fail without defaulting to equity.
- `510500.SS` selects `fund.v1` and no company-fundamentals requirement.
- The generic `market.return_20d.*` rules are equity-only.
- A fund without a fund-specific registered rule completes with an Analysis
  Outcome.
- Index acceptance has an explicit Capability Profile or fails closed before
  rule selection.

## Implementation boundaries

- Extend the existing registry schema/data and atomic digest refresh path.
- Do not restore ticker-prefix identity guessing.
- Do not invent a broad fund/index rule catalog in this ticket.

## Required tests

- Production registry resolution for `510500.SS` and aliases.
- Refresh category mapping for equity, fund, index, unknown, and ambiguous rows.
- Fund analyst-routing test.
- Fund/generic-rule and index/generic-rule negative tests.
- Registry rollback and digest verification.

## Comments

Follow-up to ADR-0011, issue 03, and the missing controlled-fund acceptance path.

## Resolution

- The digest-pinned production registry now resolves `510500.SS`, `510500.SH`,
  and unambiguous `510500` to the same authoritative Fund identity.
- Registry refresh keeps equity, fund, and index categories explicit; company
  and A-share sources cannot manufacture non-equity rows, and unknown or
  ambiguous categories fail closed.
- The generic 20-day return Strategy Rules are equity-only. Funds use the
  `fund.v1` Capability Profile without company fundamentals and complete with an
  Analysis Outcome when no Fund Strategy Rule applies.
- Index routing requires an explicit Capability Profile and cannot inherit the
  generic equity rule.
- Production alias resolution, category mapping, analyst routing, rule
  negatives, digest verification, and rollback regressions pass.

Verification:

- Full suite: 1,392 passed, 2 expected skips, 71 subtests passed.
- Ruff and `git diff --check` passed.

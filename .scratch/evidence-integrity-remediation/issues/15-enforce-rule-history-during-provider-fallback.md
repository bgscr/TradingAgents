# Enforce Strategy Rule history during provider fallback

Status: completed

Blocked by: 09, 10

## Problem

The production evidence builder requests a snapshot with
`minimum_history_rows=1`, while the registered 20-day return rule needs 21
observations. A short primary provider can therefore prevent selection of a
later provider that satisfies the rule.

## Settled direction

Resolve authoritative Instrument Identity and the Decision Horizon first. Use
`DecisionPolicyEngine.preflight_minimum_history_rows()` to derive the acquisition
floor and pass it into the existing `AcquisitionController` candidate-acceptance
path.

## Acceptance

- A one-row primary and 21-row secondary selects the secondary as the
  Authoritative Market Snapshot.
- A primary with exactly the required rows may be selected.
- If all valid candidates are short, retain the longest for diagnostics, emit
  `insufficient_history`, and publish no Trading Decision.
- Malformed/no-data candidates remain distinct from valid-but-short candidates.
- No shorter-window result is exposed under a longer-window canonical identity.
- The production path no longer hardcodes a one-row requirement.

## Implementation boundaries

- Reuse `AcquisitionController.accept_candidate` and fallback-candidate
  selection.
- Do not add another provider loop.
- Keep the provider order from ADR-0002.

## Required tests

- One-row primary, 21-row secondary.
- Exactly-required primary.
- All candidates short.
- Short primary followed by malformed and valid candidates.
- No applicable Strategy Rule.
- Admission and Decision Gate history enforcement remains intact.

## Comments

Follow-up to ADR-0013 and the 2026-07-22 standards review.

## Resolution

- The production evidence path derives the snapshot history floor from
  `DecisionPolicyEngine.preflight_minimum_history_rows()` for the resolved
  Instrument Identity and Decision Horizon.
- The graph passes its injected policy and horizon into evidence acquisition,
  so custom policies do not silently fall back to the production rule floor.
- Existing candidate acceptance now continues past valid-but-short and
  malformed providers, selects the first sufficient snapshot, and retains the
  longest valid short candidate for fail-closed diagnostics when none qualify.
- Named regressions cover one-row primary to sufficient fallback,
  exactly-required primary, all candidates short, short then malformed then
  valid, no applicable Strategy Rule, and admission/Decision Gate history
  enforcement.

Verification:

- Ticket-focused history regressions passed.
- Full suite: 1,392 passed, 2 expected skips, 71 subtests passed.
- Ruff and `git diff --check` passed.

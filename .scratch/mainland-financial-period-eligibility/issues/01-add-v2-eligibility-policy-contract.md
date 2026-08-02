# 01 — Add the v2 mainland financial eligibility-policy contract

**Parent specification:** [Mainland financial reporting-period eligibility and current-only indicators](../spec.md)

**What to build:** Establish one closed, versioned policy that a corrected
qualified shadow run can resolve, serialize, hash, restore, and inspect before
financial selection begins. The observable slice is a safe
`qualified_v1_shadow` plan whose deterministic projection identifies
`mainland-financial-period-eligibility-v2`, its mainland filing calendar,
publication-state vocabulary, and every downstream contract identity that must
share the policy. Legacy planning remains unchanged and authoritative
`qualified_v1` remains unavailable.

**Blocked by:** None — can start immediately.

**Status:** ready-for-agent

## Observable behavior

- Resolving a corrected qualified shadow plan produces a closed, secret-free
  projection with `eligibility_policy_version` exactly
  `mainland-financial-period-eligibility-v2`.
- The policy deterministically represents annual/reporting period ends, the
  four mainland publication deadlines, inclusive `ann_date` cutoff semantics,
  conservative deadline-day behavior, and the five publication states:
  `published_eligible`, `announced_after_cutoff`, `not_yet_eligible`,
  `publication_overdue_unproven`, and `pre_listing`.
- The plan signature commits to the eligibility policy, deadline table,
  completeness declaration, selection contract, manifest/checkpoint/audit/report
  projection versions, and shadow authority disposition. Changing any committed
  identity changes the signature; row, mapping, or construction order does not.
- Unknown policy fields or values fail the boundary-strict contract. No missing
  policy field is interpreted as v2.
- Legacy plan resolution and safe projection remain byte-for-byte compatible
  with their existing contract. A v2 shadow plan remains
  `shadow_non_authoritative`; authoritative `qualified_v1` cannot be enabled.

## Acceptance criteria

- [ ] A deterministic contract fixture constructs, serializes, hashes, restores,
  and revalidates the v2 policy and safe routing-plan projection.
- [ ] The annual/reporting calendar and deadlines are closed policy data, not
  adapter-local constants or caller-supplied arbitrary dates.
- [ ] Every v2 consumer identity required by the parent spec is included in the
  plan/signature commitment.
- [ ] Permuting input mapping/row order leaves canonical serialization and plan
  signature unchanged; changing one policy/deadline/contract identity changes
  them.
- [ ] Missing, unknown, tampered, or contradictory policy data fails typed before
  provider or model work and exposes no token, payload, or raw exception.
- [ ] Default legacy behavior and its existing projection remain unchanged.
- [ ] `qualified_v1_shadow` is explicitly non-authoritative and no preflight or
  configuration path makes authoritative `qualified_v1` available.

## Acceptance IDs

- **PE14 (contract-binding slice):** establishes the single policy identity that
  routing, completeness, selection, checkpoint, audit, and report must later
  carry end to end.
- **PE13 (authority invariant):** establishes the plan-level
  `shadow_non_authoritative` and qualified-authority-disabled invariants used by
  the later rollout slice.

## Deterministic regressions

- Fixed-clock round-trip for v2 policy and qualified shadow plan.
- Property/parameterized cases for all standard period-end/deadline mappings,
  including year-end belonging to both annual and reporting sequences.
- Canonical signature stability under shuffled construction order.
- Signature sensitivity for every committed downstream contract identity.
- Closed-model rejection for unknown state/deadline values and missing v2
  identity.
- Legacy plan and safe-projection regression fixtures unchanged.
- Zero provider/model call assertion for invalid or authoritative-qualified
  preflight.

## Checkpoint and migration compatibility

- This ticket uses additive v2 types and projections; it does not rewrite or
  migrate any checkpoint, report, manifest, artifact, provider row, snapshot,
  or pin.
- Existing legacy and qualified-v1 records remain readable under their recorded
  contracts. Compatibility decisions for resuming them under v2 are deferred to
  ticket 06.
- Do not default an absent policy field to v2 and do not change existing legacy
  plan signatures.

## Explicit non-goals

- Constructing five/eight target sets or selecting a financial period.
- Implementing current-only Source Fact eligibility, family aggregation,
  revision selection, checkpoint migration, or final report rendering.
- Live-provider qualification, provider calls, credentials, or raw diagnosis
  payloads.
- Lowering 100% critical-field or 90% declared-core gates.
- Fabricating 2026-06-30 data or treating `ann_date` as `f_ann_date`.
- Changing BaoStock authority, the AKShare → BaoStock → Yahoo daily order,
  Strategy Rules, Evidence Admission, or Decision Gate behavior.

## Verification commands

```powershell
pytest -q tests/test_financial_period_contracts.py tests/test_qualified_routing_rollout.py
pytest -q -m "not integration"
ruff check .
git diff --check
```

## Definition of done

- [ ] The v2 policy is closed, canonical, safe, round-trippable, and committed by
  a deterministic qualified shadow plan signature.
- [ ] Legacy compatibility and shadow non-authority tests pass with zero
  provider/model work.
- [ ] Spec Compliance review confirms the exact policy identity, calendar,
  states, deadlines, and authority boundary.
- [ ] Code Quality review finds no Critical or Important contract, secret-safety,
  determinism, or maintainability issue.
- [ ] Only the smallest coherent contract/plan/test changes are present; all
  historical state remains untouched.

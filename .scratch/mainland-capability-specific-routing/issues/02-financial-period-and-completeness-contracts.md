# 02 — Add provider-neutral financial period and completeness contracts

**What to build:** Give every acquired financial dataset an immutable, provider-neutral representation that can distinguish company type, statement/ratio capability, period, filing lineage, coverage, artifacts, and typed rejection. The contracts must make a complete bank or non-bank period independently verifiable without allowing response-dependent schemas or ratio payloads to masquerade as statements.

**Blocked by:** None — can start immediately.

**Status:** ready-for-agent

## Observable behavior

- Closed, versioned contracts cover capability, statement, frequency, ratio family, Financial Company Type, Financial Period Identity, original/normalized values and units, filing metadata, provider artifact identity, acquisition manifest, period candidates/selections, conflicts, completeness, and since-listing exceptions.
- Declared bank and non-bank critical/core field sets enforce 100% critical and at least 90% qualified core coverage. Normal issuers target five annual and eight reporting periods; a recent issuer passes only by covering every eligible post-listing period from authoritative listing metadata.
- `ann_date`, `f_ann_date`, `report_type`, `comp_type`, and `update_flag` remain distinct. A binary update flag is not a stable restatement identity, and announcement date is never substituted for first-publication date.
- Unknown/contradictory company type, ambiguous unit, incompatible currency/scope/period, capability mismatch, critical conflict, insufficient completeness, and unsupported since-listing coverage have closed typed reasons while their artifacts remain retainable.

## Acceptance criteria

- [ ] **CR8–CR10:** Bank/non-bank periods use declared company-type cores, reject unknown or contradictory classifications, and assess exactly the latest eligible five annual/eight reporting targets.
- [ ] **CR11:** The typed since-listing exception counts only authoritative post-listing expected periods and fails for any missing eligible period or pre-listing substitution.
- [ ] **CR12–CR13:** Incompatible identity dimensions are never silently merged; equivalent overlap remains unselected, while a critical conflict affects only its period and blocks its Source Facts.
- [ ] **CR14:** All filing fields survive contract round-trip, and `update_flag` alone never claims stable restatement lineage.
- [ ] **CR18:** A ratio-family artifact presented to a statement contract returns typed `capability_mismatch`.
- [ ] Contract serialization, hashing, and row-order tests are deterministic and exclude payload bodies, credentials, raw exceptions, prose, process IDs, and call order from canonical identities.
- [ ] Compatibility tests prove legacy ledger/checkpoint/report values remain readable and parent **AC3–AC5, AC11, and AC13** contracts are unchanged.

## Verification commands

```powershell
pytest -q tests/test_canonical_evidence_contracts.py tests/test_financial_tool_dispatcher.py tests/test_checkpoint_resume.py tests/test_reporting.py tests/test_evidence_artifacts.py
pytest -q -m "not integration"
ruff check .
git diff --check
```

## Definition of done

- [ ] Every contract is closed, versioned, canonical, and documented through executable deterministic tests.
- [ ] Bank, mature non-bank, Shanghai, Shenzhen, and recent-listing fixtures prove the gates and typed rejection taxonomy.
- [ ] Contracts are additive and preserve legacy readers; no payload, token, local qualification cache, or raw provider error is committed.
- [ ] Focused and compatibility commands pass, with no routing, provider SDK, Strategy Rule, Decision Gate, Evidence Admission, or dependency change in this ticket.


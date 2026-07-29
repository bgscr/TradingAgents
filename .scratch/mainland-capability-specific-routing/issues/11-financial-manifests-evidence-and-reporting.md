# 11 — Add financial manifests, evidence disposition, reporting, and artifact retention

**What to build:** Make every qualified financial acquisition auditable independently of what the model cites. Checkpoints, audits, and reports must retain the safe plan, attempts, artifacts, candidates, selections, completeness, and typed degradation while canonical Evidence Admission remains the only path from eligible selected periods to Source Facts.

**Blocked by:** 02 — Add provider-neutral financial period and completeness contracts; 10 — Implement capability-specific routing and deterministic per-period selection.

**Status:** ready-for-agent

## Observable behavior

- Versioned dispatch ledger, acquisition manifest, checkpoint projection, audit envelope, and deterministic completeness tables reference every safe plan/artifact/outcome/attempt/candidate/rejection/selection and final rendering identity.
- Successfully decoded artifacts are retained even when all periods are rejected; transport failures retain typed outcome/attempt evidence. Accepted but uncited artifacts remain present.
- Only explicitly selected, contract-valid, as-of-eligible projections may become Source Facts. Unselected, rejected, supplemental, current-only, or merely complete data cannot cross Evidence Admission automatically.
- Dispositions distinguish strict-PIT-eligible, current-only, Degraded, Insufficient, and Conflicted. Strict no-lookahead requires eligible `f_ann_date` and revision binding; Decision Gate and Decision Assertion requirements do not change.
- CLI/report/audit/checkpoint output is deterministic and contains no provider payload body, token/digest, credential, or raw exception text.

## Acceptance criteria

- [ ] **CR14:** Filing metadata and safe revision identity survive manifest, checkpoint, audit, and report round-trips without false restatement claims.
- [ ] **CR28–CR29:** All artifacts and complete manifests survive independent of model citations; only admitted selected/PIT-eligible projections can create Source Facts.
- [ ] **CR30–CR31:** Hard-gate exhaustion renders Insufficient Evidence/typed unavailable; optional breadth may render explicit Degraded Evidence only without weakening Decision Assertion requirements.
- [ ] **CR32:** Qualified checkpoint round-trip restores plan, cache, attempts, artifacts, candidates, selections, completeness, dispositions, and final rendering exactly without provider calls.
- [ ] **CR34:** CLI, report, checkpoint, audit, and strict replay projections are deterministic, secret-safe, payload-free, and backward-readable.
- [ ] Parent **AC1, AC3–AC5, AC9–AC13**, canonical Evidence Admission, Decision Gate, and immutable legacy artifact/report/checkpoint behavior remain unchanged.

## Verification commands

```powershell
pytest -q tests/test_reporting.py tests/test_checkpoint_resume.py tests/test_evidence_artifacts.py tests/test_evidence_gates.py tests/test_admitted_evidence_binding.py
pytest -q tests/test_decision_audit.py tests/test_recorded_evidence_replay.py tests/test_financial_tool_dispatcher.py tests/test_tool_evidence_envelopes.py
pytest -q -m "not integration"
ruff check .
git diff --check
```

## Definition of done

- [ ] Deterministic selected/unselected/rejected/current-only/supplemental/conflicted fixtures prove artifact retention and evidence disposition independently of model claims.
- [ ] Coverage tables report physical attempts, company type, metadata, period/provider selection, and typed reasons without unsafe data.
- [ ] Existing reports, snapshots, pins, checkpoints, provider records, and immutable artifacts remain readable and are never rewritten or deleted.
- [ ] Focused and compatibility commands pass; review confirms acquisition completeness cannot bypass Evidence Admission or alter Decision Gate safety.


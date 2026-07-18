# Evidence-gate shadow replay — 2026-07-17

## Decision

The Issue 05 promotion criteria are satisfied. All four audited failure
reconstructions block at the intended public gate, and all three recent
partial-data reconstructions remain decision-capable with degraded coverage.
There were no unexpected blocks, so the replay does not justify weakening any
evidence rule or adding a legacy exception.

## Method

The archived runs predate typed material-claim IDs. Replaying their complete
free-text reports as though they were structured evidence would create a false
result: the new decision gate would correctly reject those reports for having
no registered claim IDs and for containing unsupported numeric precision.

Instead, this replay reconstructs reviewed `EvidenceState` and `DraftThesis`
objects at the confirmed pure admission/decision-gate seam:

- Audited contradictions are represented as source-linked, conflicted material
  claims only when the historical thesis used them.
- A missing trustworthy price source is represented by the absence of an
  Authoritative Market Snapshot.
- Recent partial-data runs use one exact claim from their accepted BaoStock
  snapshot and five unavailable optional China-local sources, matching the
  observed degraded-source shape. Suspicious unsupported precision elsewhere
  in the historical reports is deliberately not certified by the replay.
- Expected results were written from the audit before the current gate results
  were evaluated.

The replay used the current implementations of `evaluate_admission_gate` and
`evaluate_decision_gate` with the default 200-row admission threshold.

## Corpus

Audited failures:

- `000725.SZ`, analysis `2026-07-15`, run `20260714_090803`
- `000021.SZ`, analysis `2026-07-14`, run `20260714_083927`
- `512210.SH`, analysis `2026-07-13`, run `20260713_170107`
- The separately audited `000725.SZ` provider/effective-date conflict

Recent partial-data sample:

- `600360.SS`, analysis `2026-07-16`, run `20260715_095304`
- `002119.SZ`, analysis `2026-07-15`, run `20260715_091411`
- `600895.SS`, analysis `2026-07-15`, run `20260715_080855`

## Results

| Case | Expected admission | Actual admission | Expected decision | Actual decision | Actual readiness | Coverage | Review |
|---|---:|---:|---:|---:|---|---:|---|
| `000725-monetary` | admit | admit | block | block | conflicted | 66.7% | Expected: the `50亿元~55亿元` 10x mistranslation and unsupported replacement estimate are material to the draft. |
| `000021-ohlc-money` | admit | admit | block | block | conflicted | 50.0% | Expected: the impossible `59.88` open above the `53.99` high and inconsistent `200.62亿元` normalization are both used conflicted premises. |
| `512210-missing-price` | block | block | n/a | n/a | insufficient | 50.0% | Expected: no Authoritative Market Snapshot exists, so debate is not admitted. |
| `000725-provider-date` | admit | admit | block | block | conflicted | 66.7% | Expected: exact values `6.83`, `7.02`, and `8.00` mix provider/effective-date provenance. |
| `600360.SS-degraded` | admit | admit | permit | permit | degraded | 28.6% | Expected: accepted `2026-07-15` BaoStock close `14.31` supports the reconstructed premise; five unused optional sources are unavailable. Confidence is low, independently of direction. |
| `002119.SZ-degraded` | admit | admit | permit | permit | degraded | 28.6% | Expected: accepted `2026-07-14` BaoStock close `29.99` supports the reconstructed premise; five unused optional sources are unavailable. Confidence is low, independently of direction. |
| `600895.SS-degraded` | admit | admit | permit | permit | degraded | 28.6% | Expected: accepted `2026-07-15` BaoStock close `31.61` supports the reconstructed premise; five unused optional sources are unavailable. Confidence is low, independently of direction. |

Every expected result matched the actual result. Unexpected blocks reviewed: **0**.

## Promotion outcome

- Promote fail-closed evidence enforcement to the default.
- Keep `shadow` only as an explicit temporary legacy override.
- Label every Trading Decision emitted under that override as evidence-gate
  **UNENFORCED**.
- Do not certify the complete historical decision text from the recent sample;
  exact values not emitted as source-linked material claims remain outside the
  reconstructed permitted thesis and would block if reintroduced unsupported.


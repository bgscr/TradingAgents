# 05 — Implement the Tushare financial-indicator adapter

**What to build:** Let qualified_v1 current analysis acquire Tushare `fina_indicator` histories as normalized indicator periods while explicitly preventing their qualified current-analysis coverage from being mistaken for strict point-in-time lineage.

**Blocked by:** 01 — Add the qualified_v1 Capability Routing Plan and configuration preflight; 02 — Add provider-neutral financial period and completeness contracts; 03 — Add coordinator endpoint scope and checkpoint-safe provider subrequest caching.

**Status:** ready-for-agent

## Observable behavior

- A real single-stock `fina_indicator` adapter returns only the financial-indicator capability, retains its immutable provider artifact, original fields/units, dataset identity, `ann_date`, and explicit metadata absences.
- Eight usable reporting periods with no more than 10% missingness in the declared qualified company-type field set satisfy current-analysis completeness.
- Without a separately admitted PIT filing binding, periods are current-only/degraded: they cannot support strict no-lookahead Source Facts, strict replay, or a claimed restatement chronology.
- Requests use ticket 03 coordination/cache semantics and map sanitized permission, authentication, rate-limit, empty, malformed, and upstream failures to exact typed outcomes.

## Acceptance criteria

- [ ] **CR16:** Complete indicator periods lacking `f_ann_date` may be selected only as current-only/degraded and are barred from strict PIT facts/replay until a valid filing binding exists.
- [ ] **CR17:** The adapter reports exact missing normalized families/periods so later routing can request only qualified gaps; it does not call AKShare, BaoStock, or Yahoo itself.
- [ ] **CR24–CR26:** Outcome typing, shared cooldown/single-flight behavior, and one bulk physical request per identical endpoint/symbol/range are deterministic.
- [ ] Bank and non-bank fixtures enforce declared company-type indicator sets and the eight-period/10% gate without response-derived schemas or bank waivers.
- [ ] Legacy mode never calls the adapter; Evidence Admission, Decision Gate, strict-history rules, and parent **AC4–AC5, AC9–AC11, and AC13** remain unchanged.

## Verification commands

```powershell
pytest -q tests/test_tushare_pit_provider.py tests/test_financial_tool_dispatcher.py tests/test_recorded_evidence_replay.py tests/test_evidence_gates.py tests/test_provider_request_coordinator.py
pytest -q -m "not integration"
ruff check .
git diff --check
```

## Definition of done

- [ ] Deterministic complete, sparse, missing-lineage, bound-lineage, and typed provider-failure fixtures pass.
- [ ] Manifest-ready output states current-only versus PIT-eligible explicitly and never promotes coverage directly to a Source Fact.
- [ ] No live provider/model call, credential, raw payload/error, VIP endpoint, hidden retry, or route selection is added.
- [ ] Focused and compatibility commands pass with no strict no-lookahead overclaim.


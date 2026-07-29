# 03 — Add coordinator endpoint scope and checkpoint-safe provider subrequest caching

**What to build:** Ensure each qualified provider subrequest is coordinated, paced, counted, single-flighted, cached within its run/checkpoint, and restored without hidden network work. Tushare remains one account-scoped upstream while endpoint is an explicit capacity/cache scope, so one throttle protects the right callers without inventing independent accounts or bypassing ADR-0031.

**Blocked by:** 01 — Add the qualified_v1 Capability Routing Plan and configuration preflight; 02 — Add provider-neutral financial period and completeness contracts.

**Status:** ready-for-agent

## Observable behavior

- Add endpoint/capacity scope through an atomic additive coordinator migration. Existing rows read as scope `all`; service leases, cooldowns, circuits, priorities, attempts, audits, and legacy databases remain readable and are never rewritten.
- Canonical subrequest keys include provider, upstream/account scope, endpoint/family, Instrument Identity, range, fields, as-of date, qualification profile, normalizer, and Data Usage Mode.
- Identical in-flight or completed subrequests share one physical attempt and artifact/cache identity. Bulk statement responses can drive annual and reporting projections; BaoStock family/year/quarter keys stay independent. Terminal cache state survives checkpoint round-trip, but retained cross-run artifacts are not silently reused as fresh responses.
- One account-scoped `tushare-pro` Upstream Service Identity enforces global and endpoint cooldowns, positive operator pacing capped at the qualified profile ceiling, exact physical-attempt accounting, foreground priority, and no adapter retry loop or fallback fan-out.

## Acceptance criteria

- [ ] **CR24:** Permission, rate-limit, authentication, empty, malformed, and success outcomes remain distinct; endpoint/global cooldown behavior is deterministic and no hidden retry/fan-out occurs.
- [ ] **CR25–CR27:** Duplicate Tushare requests single-flight, bulk response projections cost one call, and BaoStock attempts equal exactly the distinct missing family/period cache keys.
- [ ] **CR32:** Checkpoint restore reproduces cache, attempts, artifacts, and outcomes without a new provider call.
- [ ] Parent **AC9–AC10** exact-attempt, budget, Retry-After, cooldown, and sequential-fallback behavior remains unchanged, satisfying **ADR-0031**.
- [ ] Parent **AC6, AC11, and AC13** regression coverage proves atomic migration, cross-process durability, legacy readability, and scoped changes.
- [ ] All tests use fixed clocks, event barriers, fake transports, and deterministic payload fixtures; no sleeps, live provider, token, or paid model call.

## Verification commands

```powershell
pytest -q tests/test_provider_request_coordinator.py tests/test_mainland_physical_attempt_coordination.py tests/test_financial_tool_dispatcher.py tests/test_checkpoint_resume.py
pytest -q tests/test_market_history_payload_gc_concurrency.py tests/test_market_history_store.py
pytest -q -m "not integration"
ruff check .
git diff --check
```

## Definition of done

- [ ] The additive migration, cache, cooldown scopes, attempt events, and checkpoint state are deterministic and backward-readable.
- [ ] No provider SDK performs hidden retries; every physical attempt passes through the Provider Request Coordinator and respects ADR-0031.
- [ ] Secrets, payload bodies, token digests, and raw exception text never enter keys, persistence, checkpoints, reports, or errors.
- [ ] Focused, migration, and compatibility commands pass; no provider routing order or production adapter behavior is added in this ticket.


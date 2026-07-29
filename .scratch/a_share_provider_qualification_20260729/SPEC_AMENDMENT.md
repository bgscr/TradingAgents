# Proposed specification amendment: mainland capability-qualified acquisition

## Objective

Replace category-wide mainland provider fallback with capability- and completeness-qualified acquisition while preserving the current market-snapshot order until acceptance criteria pass.

## Requirements

1. Define separate provider capabilities for adjusted OHLCV, raw OHLCV, adjustment factors, trading status, ST/name/delisting events, each full statement/frequency, ratio families, announcement dates, first-publication dates, restatement/update identity, and statement metadata.
2. A provider may enter a route only after a live entitlement probe and two-run qualification for that capability. Documentation-only capability is `not_qualified`.
3. Selected BaoStock financial metrics must return `CAPABILITY_PARTIAL_METRICS_NOT_FULL_STATEMENT` for statement requests and continue fallback.
4. Implement AKShare-Sina annual/quarterly statement adapters and company-type-specific normalization. Preserve `报告日`, `公告日期`, `币种`, `类型`, and `更新日期`.
5. Implement a completeness-based AKShare/Yahoo statement merge: retain both manifests, accept only periods meeting the normalized threshold, and reject unresolved value conflicts.
6. Implement BaoStock ratio-family adapters and raw/status/factor routing with symbol/year/quarter caching. Do not make BaoStock ratio calls for fields already satisfied by a higher-priority accepted result.
7. Keep Tushare financial/status/factor/name capabilities disabled for the current token. Requalification after an entitlement change is required before enabling any individual capability.
8. Evidence must record requested periods, returned periods, usable periods, missing normalized fields, provider calls, typed failures, first-publication and update identity, unit, currency, consolidation scope, and company type.
9. A financial section is `insufficient` when any required statement lacks five annual or eight quarterly usable periods, except an explicit since-listing IPO policy. Missing first-publication or restatement lineage is at least `degraded` and bars point-in-time claims.
10. Do not change the existing market-snapshot provider order as part of this amendment's initial implementation.

## Acceptance criteria

- The four-symbol qualification cohort passes twice without credential leakage or production-state writes.
- OHLCV covers at least 99.5% of exchange sessions with raw, adjusted, factor, and dated status lineage.
- Every accepted statement has five annual and eight quarterly periods, 100% critical fields, at least 90% company-type core coverage, and complete unit/currency/consolidation/company-type metadata.
- Every point-in-time financial fact is unavailable to the decision context before its first-publication date.
- Provider revisions are accepted only with a stable update/restatement identifier.
- Bank and recent-IPO fixtures exercise company-type and since-listing exceptions.
- Ratio-only BaoStock payloads cannot satisfy statement tests.
- Tushare permission and rate failures remain typed and do not cause retries outside the current token's entitlement.

## Non-goals

- Purchasing or upgrading a data account.
- Globally changing provider priority.
- Treating an AKShare package upgrade as a capability improvement without measured coverage or stability gains.

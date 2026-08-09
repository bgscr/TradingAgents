# 02E — Financial-indicator semantic metadata contracts

**Parent specification:** [Mainland qualified routing production composition factory](../spec.md)

**What to build:** Add the provider-neutral semantic contracts that let a
qualified shadow run distinguish complete indicator values from compatible
indicator evidence. A caller can classify every currently declared normalized
family-field pair from one closed policy, retain raw provider `UNKNOWN`
separately from semantic assessment, produce a deterministic compatibility
identity with typed reasons, and bind the policy into Capability Routing Plan
payload 1.2 without changing routing, completeness, financial-period
eligibility, current-only, strict-PIT, or Evidence Admission behavior.

**Blocked by:** 01 — Add the production composition contract and immutable
blueprint; accepted QCF4 metadata-compatibility prototype report.

**Status:** ready-for-agent

## Observable behavior

- Given any currently declared normalized indicator family-field pair and its
  financial-ratio declaration version, `financial-indicator-field-semantics-v1`
  returns one immutable classification containing field class, canonical unit/
  basis, value-currency applicability, filing-scope materiality, required
  provider evidence, current-only eligibility, and strict-PIT eligibility.
- The registry is closed and complete: exactly 70 family-field pairs and 68
  unique normalized fields are classified across legacy v1, qualified Tushare
  v2, and BaoStock v1 declarations. Missing, duplicate, ambiguous, or unknown
  declarations fail closed; populated response sparsity cannot change policy.
- `financial-indicator-metadata-compatibility-v1` assesses each retained field
  with the closed states `PROVEN`, `UNPROVEN`, or `NOT_APPLICABLE`, while keeping
  raw provider markers such as `UNKNOWN` or `XXX` in a separate field.
- A deterministic assessment identity commits to candidate, provider artifact,
  period, family, retained field set, Instrument Identity, requested capability
  metadata, declaration version, field-semantics version, raw marker/value,
  assessment state, safe evidence origin, expected semantic value, and canonical
  typed reason.
- Capability Routing Plan payload 1.2 pins
  `financial-indicator-field-semantics-v1`,
  `financial-indicator-metadata-compatibility-v1`, `financial-manifest-v3`, and
  `financial-period-selection-v3`. Its provider routes, policy version,
  financial-period eligibility v2, completeness policy/thresholds, and
  `mainland-routing-plan:v1:<sha256>` signature namespace stay unchanged; the
  digest changes because the new bindings are material.

## Acceptance criteria

- [ ] The registry contains the following complete family-field classification
  with no additional currently normalized field:
  - Profit / Per-share: `earnings_per_share`.
  - Profit / Percentage/rate: `ebitda_margin`, `gross_margin`, `net_margin`,
    `operating_margin`, `pre_tax_margin`, `profit_to_cost`, `return_on_assets`,
    `return_on_equity`, `return_on_invested_capital`.
  - Operation / Days/turnover-time: `accounts_payable_turnover`,
    `asset_turnover`, `cash_conversion_cycle`, `current_asset_turnover`,
    `fixed_asset_turnover`, `inventory_days`, `inventory_turnover`,
    `operating_cycle`, `receivable_days`, `receivables_turnover`.
  - Growth / Percentage/rate: `asset_growth`,
    `basic_earnings_per_share_growth`, `cash_flow_growth`, `equity_growth`,
    `gross_profit_growth`, `liability_growth`, `net_income_growth`,
    `operating_profit_growth`, `parent_net_income_growth`, `revenue_growth`,
    `return_on_equity_growth`, `total_profit_growth`,
    `working_capital_growth`.
  - Balance / Dimensionless ratio: `cash_ratio`, `current_ratio`,
    `debt_to_assets`, `debt_to_equity`, `equity_multiplier`,
    `interest_coverage`, `long_term_debt_ratio`, `quick_ratio`,
    `tangible_asset_ratio`, `working_capital_ratio`.
  - Cash flow / Dimensionless ratio: `cash_flow_adequacy`,
    `cash_flow_to_debt`, `cash_flow_to_gross_revenue`,
    `cash_flow_to_liabilities`, `cash_flow_to_revenue`,
    `current_assets_to_assets`, `interest_coverage`,
    `non_current_assets_to_assets`, `operating_cash_flow_ratio`,
    `quality_of_income`, `reinvestment_ratio`, `self_financing_ratio`,
    `tangible_asset_ratio`.
  - Cash flow / Percentage/rate: `cash_return_on_assets`,
    `free_cash_flow_margin`.
  - DuPont / Days/turnover-time: `asset_turnover_component`.
  - DuPont / Dimensionless ratio: `debt_burden`,
    `equity_multiplier_component`, `financial_leverage`, `interest_burden`,
    `parent_net_income_share`, `tax_burden`.
  - DuPont / Percentage/rate: `ebit_margin_component`,
    `net_profit_margin_component`, `operating_margin_component`,
    `return_on_assets_component`, `return_on_equity_component`.
- [ ] Per-share fields require provider-proven value currency, share unit/basis,
  announcement metadata, and proven filing scope. Dimensionless ratios and
  percentage/rate fields use canonical `RATIO`, declare value currency
  `NOT_APPLICABLE`, and require proven filing scope. Days/turnover-time fields
  use declared `DAYS` or `TURNS_PER_REPORTING_PERIOD`, declare value currency
  `NOT_APPLICABLE`, and require proven filing scope.
- [ ] The policy shape also defines fail-closed monetary, share-count, and
  `other` classes for future declarations, while confirming no currently
  normalized field belongs to those classes. BaoStock raw-only `netProfit`,
  `MBRevenue`, `totalShare`, `liqaShare`, `NRTurnDays`, and `INVTurnDays` remain
  raw artifact data and are not promoted.
- [ ] `PROVEN` means qualified provider-origin evidence proves and matches a
  material dimension; `UNPROVEN` means a material dimension lacks proof and is
  incompatible; `NOT_APPLICABLE` is permitted only when the pinned field policy
  declares the dimension semantically irrelevant.
- [ ] Raw `UNKNOWN`/`XXX` remains distinct from assessment state and is never
  converted to `PROVEN`, `NOT_APPLICABLE`, `CNY`, or `CONSOLIDATED`.
- [ ] Instrument Identity currency, normalized value unit/basis, value-currency
  applicability, and filing-consolidation scope remain separate dimensions.
  Requested currency/scope is immutable intent only and cannot become provider
  evidence.
- [ ] Filing scope is material for every currently normalized statement-derived
  indicator field. Companion-statement metadata is rejected unless a future
  qualified contract supplies an exact stable shared filing/report identity,
  including revision; provider, Instrument Identity, company type, period, or
  observation-time matches and Tushare `update_flag` are insufficient.
- [ ] Compatibility remains a hard gate separate from the unchanged 100%
  critical and at-least-90% declared-core completeness gates. Financial-period
  eligibility v2 and current-only/strict-PIT publication, observation,
  availability, and revision boundaries are unchanged.

## Exact contract and version requirements

- Add `financial-indicator-field-semantics-v1` with the complete closed registry
  above.
- Add `financial-indicator-metadata-compatibility-v1` and identity namespace
  `financial-indicator-metadata-compatibility:v1` with canonical typed reasons.
- Add Capability Routing Plan payload version `1.2` with the exact indicator
  bindings above; retain statement manifest/selection bindings at v2.
- Keep Financial Period Candidate contract `1.0`,
  `financial-period-candidate:v1`, `financial-period:v1`, and all raw candidate
  identities at v1. These raw identities retain provider markers as observed
  and gain no applicability state.
- Define the bindings required by `financial-period-selection-v3` and
  `financial-provider-candidate-v2` without implementing persistence or
  provider normalization in this ticket.

## Deterministic regressions

- Enumerate the registry and assert exactly 70 family-field pairs, 68 unique
  normalized fields, exact class/unit/applicability/scope declarations, and
  fail-closed behavior for missing, duplicate, unknown, or version-mismatched
  entries.
- Prove field classification is identical for full, sparse, empty, and
  differently ordered provider responses.
- Cover all `PROVEN`/`UNPROVEN`/`NOT_APPLICABLE` transitions, raw
  `UNKNOWN`/`XXX`, mismatches, deterministic typed reasons, identity stability,
  and identity changes for every material binding.
- Prove CNY Instrument Identity never supplies value-currency proof and that
  request currency/scope cannot enter provider-origin assessment evidence.
- Prove missing `f_ann_date` remains current-only at most and cannot produce a
  strict historical Source Fact or strict replay eligibility.
- Assert Plan 1.2 changes only the required payload bindings/digest and does not
  change provider order, completeness thresholds, financial-period eligibility
  v2, daily AKShare → BaoStock → Yahoo, or ADR-0031 coordination semantics.

## Historical compatibility

- Historical artifacts, candidates, plans, manifests, selections, reports,
  audits, checkpoints, and IDs remain readable and byte-for-byte untouched under
  their recorded contracts.
- Readers use the semantic/compatibility contract pinned in historical state;
  they never apply the new registry retroactively or rewrite old raw markers.
- New plan-1.2 writes pin the new identities. Persistence, restore, and
  old/absent-compatibility `START_NEW` behavior belong to Ticket 05B.

## Explicit non-goals

- Provider adapter/normalizer implementation, provider-specific compatibility,
  live provider/model I/O, credentials, integration tests, or copying/importing
  either prototype implementation.
- Route/order/fallback changes, provider-specific router exceptions, provider
  fan-out, completeness changes, financial-period eligibility changes, or a
  second routing authority.
- Evidence Admission rewrite or version change, Source Fact/Decision contract
  change, companion-filing inference, requested-metadata backfill, or historical
  reinterpretation.
- Changing BaoStock authority, daily AKShare → BaoStock → Yahoo routing,
  `shadow_non_authoritative`, or enabling authoritative `qualified_v1`.

## Verification commands

Run deterministic contract and policy tests only:

```powershell
pytest -q tests/test_financial_indicator_field_semantics.py tests/test_financial_indicator_metadata_compatibility.py
pytest -q tests/test_financial_capability_routing_plan.py tests/test_financial_period_contracts.py tests/test_financial_period_eligibility.py
pytest -q tests/test_admitted_evidence_binding.py tests/test_recorded_evidence_replay.py tests/test_evidence_gates.py
ruff check .
git diff --check
git status --short
```

No command may select integration tests or contact a provider or model.

## Definition of done

- [ ] The complete 70-pair/68-field registry and all semantic assessment states
  pass exhaustive deterministic contract tests.
- [ ] Plan 1.2 and compatibility identities bind exactly the required material
  inputs while unchanged namespaces and boundaries remain unchanged.
- [ ] Tests prove raw unknowns, request intent, and Instrument Identity cannot be
  promoted into provider proof, and missing material scope fails closed.
- [ ] Historical readers pass with no migration or reinterpretation.
- [ ] Spec Compliance and Code Quality review find no unresolved Critical or
  Important issue; no live traffic occurred, no prototype code was copied, and
  authoritative `qualified_v1` remains disabled.

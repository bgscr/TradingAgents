from __future__ import annotations

from datetime import date, datetime, timezone
from decimal import Decimal

import pytest
from pydantic import ValidationError

from tradingagents.dataflows.financial_contracts import (
    FinancialAcquisitionManifest,
    FinancialAcquisitionOutcome,
    FinancialCapability,
    FinancialCompanyType,
    FinancialCompanyTypeResolution,
    FinancialConsolidationScope,
    FinancialFieldValue,
    FinancialFilingMetadata,
    FinancialListingProvenance,
    FinancialPeriodCandidate,
    FinancialPeriodDisposition,
    FinancialPeriodIdentity,
    FinancialPeriodRejectionReason,
    FinancialPeriodSelection,
    FinancialProviderArtifactEntry,
    FinancialProviderArtifactIdentity,
    FinancialProviderCandidate,
    FinancialProviderDatasetIdentity,
    FinancialRatioFamily,
    FinancialRejectedPeriod,
    FinancialReportingFrequency,
    FinancialSinceListingException,
    FinancialStatementType,
    assess_financial_history_coverage,
    assess_financial_period_candidate,
    assess_financial_ratio_history_coverage,
    compare_financial_period_candidates,
    financial_ratio_field_declaration,
    financial_statement_field_declaration,
    mark_financial_period_company_type_conflicted,
    mark_financial_period_conflicted,
    resolve_financial_company_type,
)
from tradingagents.evidence import (
    AcquisitionUnavailableReason,
    IdentityProvenance,
    InstrumentIdentityEvidence,
    InstrumentKind,
)

_OBSERVED_AT = datetime(2026, 7, 29, 12, 0, tzinfo=timezone.utc)


def _instrument(*, symbol: str = "601328.SS") -> InstrumentIdentityEvidence:
    return InstrumentIdentityEvidence(
        symbol=symbol,
        venue="XSHG",
        instrument_kind=InstrumentKind.EQUITY,
        currency="CNY",
        provenance=IdentityProvenance(
            provider="fixture-registry",
            source_ref="acq.v1:identity:" + "1" * 64,
            retrieved_at="2026-07-29T00:00:00Z",
            artifact_sha256="1" * 64,
        ),
    )


def _artifact(
    *,
    payload_marker: str = "payload",
    observed_at: datetime = _OBSERVED_AT,
) -> FinancialProviderArtifactIdentity:
    dataset = FinancialProviderDatasetIdentity.create(
        provider_id="fixture_provider",
        endpoint_id="balancesheet",
        dataset_id="single_stock_history",
        schema_identity="fixture-balance-schema-v1",
    )
    return FinancialProviderArtifactIdentity.create(
        dataset=dataset,
        canonical_request={"symbol": "601328.SS"},
        provider_metadata={"report_type": "1"},
        retrieved_at=observed_at,
        observed_at=observed_at,
        payload={"fixture": payload_marker},
    )


def _candidate(
    company_type: FinancialCompanyType,
    fields: tuple[FinancialFieldValue, ...],
    *,
    resolution: FinancialCompanyTypeResolution | None = None,
    ann_date: date | None = date(2026, 4, 20),
    f_ann_date: date | None = date(2026, 4, 22),
    currency: str = "CNY",
    consolidation_scope: FinancialConsolidationScope = (
        FinancialConsolidationScope.CONSOLIDATED
    ),
    period_end: date = date(2025, 12, 31),
    frequency: FinancialReportingFrequency = FinancialReportingFrequency.ANNUAL,
    artifact_marker: str = "payload",
    instrument_identity: InstrumentIdentityEvidence | None = None,
    observed_at: datetime = _OBSERVED_AT,
) -> FinancialPeriodCandidate:
    artifact = _artifact(payload_marker=artifact_marker, observed_at=observed_at)
    filing = FinancialFilingMetadata(
        ann_date=ann_date,
        f_ann_date=f_ann_date,
        report_type="1",
        comp_type="2" if company_type is FinancialCompanyType.BANK else "1",
        update_flag="0",
        retrieved_at=observed_at,
        observed_at=observed_at,
        local_provider_revision_identity=artifact.artifact_identity,
    )
    period_identity = FinancialPeriodIdentity.create(
        instrument_identity=instrument_identity or _instrument(),
        capability=FinancialCapability.STATEMENT,
        statement_type=FinancialStatementType.BALANCE_SHEET,
        ratio_family=None,
        period_end=period_end,
        frequency=frequency,
        company_type=company_type,
        consolidation_scope=consolidation_scope,
        currency=currency,
        provider_revision_binding=artifact.artifact_identity,
    )
    return FinancialPeriodCandidate.create(
        artifact=artifact,
        period_identity=period_identity,
        filing_metadata=filing,
        company_type_resolution=resolution
        or resolve_financial_company_type(
            provider_declared_type=company_type,
            provider_declaration_qualified=True,
            classifier_metadata={},
            present_fields=tuple(field.normalized_field for field in fields),
        ),
        fields=fields,
    )


def _declared_statement_fields(
    company_type: FinancialCompanyType,
) -> tuple[FinancialFieldValue, ...]:
    declaration = financial_statement_field_declaration(
        company_type,
        FinancialStatementType.BALANCE_SHEET,
    )
    return tuple(
        FinancialFieldValue(
            provider_field=field_name,
            original_value="1",
            original_unit=declaration.normalized_unit,
            normalized_field=field_name,
            normalized_value="1",
            normalized_unit=declaration.normalized_unit,
        )
        for field_name in declaration.core_fields
    )


def _ratio_candidate(
    fields: tuple[FinancialFieldValue, ...],
    *,
    f_ann_date: date | None = date(2026, 4, 22),
    period_end: date = date(2025, 12, 31),
) -> FinancialPeriodCandidate:
    artifact = _artifact()
    filing = FinancialFilingMetadata(
        ann_date=date(2026, 4, 20),
        f_ann_date=f_ann_date,
        report_type="1",
        comp_type="1",
        update_flag="0",
        retrieved_at=_OBSERVED_AT,
        observed_at=_OBSERVED_AT,
        local_provider_revision_identity=artifact.artifact_identity,
    )
    period_identity = FinancialPeriodIdentity.create(
        instrument_identity=_instrument(),
        capability=FinancialCapability.RATIO_FAMILY,
        statement_type=None,
        ratio_family=FinancialRatioFamily.PROFIT,
        period_end=period_end,
        frequency=FinancialReportingFrequency.QUARTERLY,
        company_type=FinancialCompanyType.INDUSTRIAL_NON_BANK,
        consolidation_scope=FinancialConsolidationScope.CONSOLIDATED,
        currency="CNY",
        provider_revision_binding=artifact.artifact_identity,
    )
    return FinancialPeriodCandidate.create(
        artifact=artifact,
        period_identity=period_identity,
        filing_metadata=filing,
        company_type_resolution=resolve_financial_company_type(
            provider_declared_type=FinancialCompanyType.INDUSTRIAL_NON_BANK,
            provider_declaration_qualified=True,
            classifier_metadata={},
            present_fields=tuple(field.normalized_field for field in fields),
        ),
        fields=fields,
    )


def test_provider_artifact_identity_is_canonical_payload_free_and_material() -> None:
    dataset = FinancialProviderDatasetIdentity.create(
        provider_id="fixture_provider",
        endpoint_id="balancesheet",
        dataset_id="single_stock_history",
        schema_identity="fixture-balance-schema-v1",
    )
    reordered_dataset = FinancialProviderDatasetIdentity.create(
        schema_identity="fixture-balance-schema-v1",
        dataset_id="single_stock_history",
        endpoint_id="balancesheet",
        provider_id="fixture_provider",
    )
    first = FinancialProviderArtifactIdentity.create(
        dataset=dataset,
        canonical_request={"symbol": "601328.SS", "fields": ["b", "a"]},
        provider_metadata={"report_type": "1", "update_flag": "0"},
        retrieved_at=_OBSERVED_AT,
        observed_at=_OBSERVED_AT,
        payload=[
            {"period_end": "2025-12-31", "total_assets": "10"},
            {"period_end": "2024-12-31", "total_assets": "9"},
        ],
    )
    reordered = FinancialProviderArtifactIdentity.create(
        dataset=reordered_dataset,
        canonical_request={"fields": ["a", "b"], "symbol": "601328.SS"},
        provider_metadata={"update_flag": "0", "report_type": "1"},
        retrieved_at=_OBSERVED_AT,
        observed_at=_OBSERVED_AT,
        payload=[
            {"total_assets": "9", "period_end": "2024-12-31"},
            {"total_assets": "10", "period_end": "2025-12-31"},
        ],
    )
    changed = FinancialProviderArtifactIdentity.create(
        dataset=dataset,
        canonical_request={"symbol": "601328.SS", "fields": ["a", "b"]},
        provider_metadata={"report_type": "1", "update_flag": "0"},
        retrieved_at=_OBSERVED_AT,
        observed_at=_OBSERVED_AT,
        payload=[
            {"period_end": "2024-12-31", "total_assets": "9"},
            {"period_end": "2025-12-31", "total_assets": "11"},
        ],
    )
    ordered_range = FinancialProviderArtifactIdentity.create(
        dataset=dataset,
        canonical_request={
            "symbol": "601328.SS",
            "date_range": ["2024-01-01", "2025-12-31"],
        },
        provider_metadata={"report_type": "1", "update_flag": "0"},
        retrieved_at=_OBSERVED_AT,
        observed_at=_OBSERVED_AT,
        payload=[{"period_end": "2025-12-31", "total_assets": "10"}],
    )
    reversed_range = FinancialProviderArtifactIdentity.create(
        dataset=dataset,
        canonical_request={
            "symbol": "601328.SS",
            "date_range": ["2025-12-31", "2024-01-01"],
        },
        provider_metadata={"report_type": "1", "update_flag": "0"},
        retrieved_at=_OBSERVED_AT,
        observed_at=_OBSERVED_AT,
        payload=[{"period_end": "2025-12-31", "total_assets": "10"}],
    )
    operational_noise = FinancialProviderArtifactIdentity.create(
        dataset=dataset,
        canonical_request={
            "symbol": "601328.SS",
            "fields": ["a", "b"],
            "process_id": 9876,
            "call_order": 4,
            "api_token": "fixture-secret-request",
            "model_prose": "not identity material",
        },
        provider_metadata={
            "report_type": "1",
            "update_flag": "0",
            "authorization": "Bearer fixture-secret-metadata",
            "process_id": 1234,
        },
        retrieved_at=_OBSERVED_AT,
        observed_at=_OBSERVED_AT,
        payload=[
            {
                "period_end": "2024-12-31",
                "total_assets": "9",
                "model_prose": "not identity material",
                "process_id": 5,
            },
            {
                "period_end": "2025-12-31",
                "total_assets": "10",
                "api_token": "fixture-secret-payload",
            },
        ],
    )

    assert dataset == reordered_dataset
    assert first == reordered
    assert first == operational_noise
    assert first.artifact_identity.startswith("financial-provider-artifact:v1:")
    assert changed.artifact_identity != first.artifact_identity
    assert ordered_range.artifact_identity != reversed_range.artifact_identity
    serialized = first.model_dump(mode="json")
    assert "payload" not in serialized
    assert "provider_metadata" not in serialized
    assert "fixture-secret" not in first.model_dump_json()
    with pytest.raises(ValidationError):
        first.artifact_identity = "financial-provider-artifact:v1:" + "0" * 64


def test_financial_period_identity_and_filing_contract_preserve_material_lineage() -> None:
    assert {item.value for item in FinancialCompanyType} == {
        "bank",
        "industrial/non-bank",
        "insurance",
        "securities",
        "other-financial",
        "unknown",
    }
    filing = FinancialFilingMetadata(
        ann_date=date(2026, 4, 20),
        f_ann_date=date(2026, 4, 22),
        report_type="1",
        comp_type="2",
        update_flag="1",
        retrieved_at=_OBSERVED_AT,
        observed_at=_OBSERVED_AT,
        local_provider_revision_identity=(
            "financial-provider-artifact:v1:" + "2" * 64
        ),
    )
    identity = FinancialPeriodIdentity.create(
        instrument_identity=_instrument(),
        capability=FinancialCapability.STATEMENT,
        statement_type=FinancialStatementType.BALANCE_SHEET,
        ratio_family=None,
        period_end=date(2025, 12, 31),
        frequency=FinancialReportingFrequency.ANNUAL,
        company_type=FinancialCompanyType.BANK,
        consolidation_scope=FinancialConsolidationScope.CONSOLIDATED,
        currency="CNY",
        provider_revision_binding=filing.local_provider_revision_identity,
    )
    changed_currency = FinancialPeriodIdentity.create(
        instrument_identity=_instrument(),
        capability=FinancialCapability.STATEMENT,
        statement_type=FinancialStatementType.BALANCE_SHEET,
        ratio_family=None,
        period_end=date(2025, 12, 31),
        frequency=FinancialReportingFrequency.ANNUAL,
        company_type=FinancialCompanyType.BANK,
        consolidation_scope=FinancialConsolidationScope.CONSOLIDATED,
        currency="USD",
        provider_revision_binding=filing.local_provider_revision_identity,
    )
    display_only_change = FinancialPeriodIdentity.create(
        instrument_identity=_instrument().model_copy(
            update={"display_name": "model prose must not define identity"}
        ),
        capability=FinancialCapability.STATEMENT,
        statement_type=FinancialStatementType.BALANCE_SHEET,
        ratio_family=None,
        period_end=date(2025, 12, 31),
        frequency=FinancialReportingFrequency.ANNUAL,
        company_type=FinancialCompanyType.BANK,
        consolidation_scope=FinancialConsolidationScope.CONSOLIDATED,
        currency="CNY",
        provider_revision_binding=filing.local_provider_revision_identity,
    )
    field = FinancialFieldValue(
        provider_field="total_assets",
        original_value="1200000000000.00",
        original_unit="CNY",
        normalized_field="total_assets",
        normalized_value="1200000000000.00",
        normalized_unit="CNY_BASE_UNIT",
    )

    assert identity.period_identity.startswith("financial-period:v1:")
    assert changed_currency.period_identity != identity.period_identity
    assert display_only_change.period_identity == identity.period_identity
    assert filing.ann_date != filing.f_ann_date
    assert filing.is_strict_pit_eligible(as_of_date=date(2026, 7, 29)) is True
    assert filing.provider_restatement_lineage_established is False
    assert field.original_value == field.normalized_value
    with pytest.raises(ValidationError):
        FinancialFilingMetadata(
            **{
                **filing.model_dump(mode="json"),
                "provider_filing_revision_id": filing.update_flag,
            }
        )

    ratio_identity = FinancialPeriodIdentity.create(
        instrument_identity=_instrument(),
        capability=FinancialCapability.RATIO_FAMILY,
        statement_type=None,
        ratio_family=FinancialRatioFamily.DUPONT,
        period_end=date(2025, 12, 31),
        frequency=FinancialReportingFrequency.QUARTERLY,
        company_type=FinancialCompanyType.BANK,
        consolidation_scope=FinancialConsolidationScope.CONSOLIDATED,
        currency="CNY",
        provider_revision_binding=filing.local_provider_revision_identity,
    )
    assert ratio_identity.statement_type is None


def test_statement_completeness_uses_declared_bank_and_non_bank_cores() -> None:
    bank_declaration = financial_statement_field_declaration(
        FinancialCompanyType.BANK,
        FinancialStatementType.BALANCE_SHEET,
    )
    industrial_declaration = financial_statement_field_declaration(
        FinancialCompanyType.INDUSTRIAL_NON_BANK,
        FinancialStatementType.BALANCE_SHEET,
    )
    bank_fields = _declared_statement_fields(FinancialCompanyType.BANK)
    industrial_fields = _declared_statement_fields(
        FinancialCompanyType.INDUSTRIAL_NON_BANK
    )

    bank = assess_financial_period_candidate(
        _candidate(FinancialCompanyType.BANK, bank_fields),
        requested_capability=FinancialCapability.STATEMENT,
        requested_statement_type=FinancialStatementType.BALANCE_SHEET,
        requested_ratio_family=None,
        expected_instrument_identity=_instrument(),
        expected_frequency=FinancialReportingFrequency.ANNUAL,
        expected_currency="CNY",
        expected_consolidation_scope=FinancialConsolidationScope.CONSOLIDATED,
        pit_as_of_date=date(2026, 7, 29),
    )
    industrial = assess_financial_period_candidate(
        _candidate(FinancialCompanyType.INDUSTRIAL_NON_BANK, industrial_fields),
        requested_capability=FinancialCapability.STATEMENT,
        requested_statement_type=FinancialStatementType.BALANCE_SHEET,
        requested_ratio_family=None,
        expected_instrument_identity=_instrument(),
        expected_frequency=FinancialReportingFrequency.ANNUAL,
        expected_currency="CNY",
        expected_consolidation_scope=FinancialConsolidationScope.CONSOLIDATED,
        pit_as_of_date=date(2026, 7, 29),
    )

    assert "customer_deposits" in bank_declaration.critical_fields
    assert "customer_deposits" not in industrial_declaration.core_fields
    assert "inventory" in industrial_declaration.core_fields
    assert "inventory" not in bank_declaration.core_fields
    assert bank.disposition is FinancialPeriodDisposition.ACCEPTED
    assert industrial.disposition is FinancialPeriodDisposition.ACCEPTED
    assert bank.critical_coverage == 1
    assert bank.core_coverage == 1


def test_unknown_or_contradictory_company_type_is_rejected() -> None:
    unknown = assess_financial_period_candidate(
        _candidate(FinancialCompanyType.UNKNOWN, ()),
        requested_capability=FinancialCapability.STATEMENT,
        requested_statement_type=FinancialStatementType.BALANCE_SHEET,
        requested_ratio_family=None,
        expected_instrument_identity=_instrument(),
        expected_frequency=FinancialReportingFrequency.ANNUAL,
        expected_currency="CNY",
        expected_consolidation_scope=FinancialConsolidationScope.CONSOLIDATED,
        pit_as_of_date=date(2026, 7, 29),
    )
    contradictory_resolution = resolve_financial_company_type(
        provider_declared_type=FinancialCompanyType.BANK,
        provider_declaration_qualified=True,
        classifier_metadata={"industry": "industrial"},
        present_fields=("customer_deposits", "inventory"),
    )
    contradictory = assess_financial_period_candidate(
        _candidate(
            FinancialCompanyType.BANK,
            _declared_statement_fields(FinancialCompanyType.BANK),
            resolution=contradictory_resolution,
        ),
        requested_capability=FinancialCapability.STATEMENT,
        requested_statement_type=FinancialStatementType.BALANCE_SHEET,
        requested_ratio_family=None,
        expected_instrument_identity=_instrument(),
        expected_frequency=FinancialReportingFrequency.ANNUAL,
        expected_currency="CNY",
        expected_consolidation_scope=FinancialConsolidationScope.CONSOLIDATED,
        pit_as_of_date=date(2026, 7, 29),
    )

    assert unknown.disposition is FinancialPeriodDisposition.REJECTED
    assert contradictory.disposition is FinancialPeriodDisposition.REJECTED
    assert unknown.rejection_reasons == (
        FinancialPeriodRejectionReason.AMBIGUOUS_COMPANY_TYPE,
    )
    assert contradictory_resolution.classifier_version == (
        "financial-company-type-classifier-v1"
    )
    assert contradictory_resolution.classifier_inputs
    assert FinancialPeriodRejectionReason.AMBIGUOUS_COMPANY_TYPE in (
        contradictory.rejection_reasons
    )


def test_company_type_resolution_rejects_tampering_and_unsafe_metadata() -> None:
    resolution = resolve_financial_company_type(
        provider_declared_type=FinancialCompanyType.BANK,
        provider_declaration_qualified=True,
        classifier_metadata={"industry": "bank"},
        present_fields=("customer_deposits",),
    )

    with pytest.raises(ValidationError):
        FinancialCompanyTypeResolution.model_validate(
            {
                **resolution.model_dump(mode="json"),
                "provider_declared_type": FinancialCompanyType.INSURANCE.value,
            }
        )

    with pytest.raises(ValueError) as exc_info:
        resolve_financial_company_type(
            provider_declared_type=None,
            provider_declaration_qualified=False,
            classifier_metadata={
                "authorization": "Bearer fixture-secret-classifier"
            },
            present_fields=(),
        )
    assert "fixture-secret-classifier" not in str(exc_info.value)


def test_statement_period_requires_all_critical_and_ninety_percent_core() -> None:
    all_fields = _declared_statement_fields(
        FinancialCompanyType.INDUSTRIAL_NON_BANK
    )

    def assess_without(*missing: str):
        fields = tuple(
            field for field in all_fields if field.normalized_field not in missing
        )
        return assess_financial_period_candidate(
            _candidate(FinancialCompanyType.INDUSTRIAL_NON_BANK, fields),
            requested_capability=FinancialCapability.STATEMENT,
            requested_statement_type=FinancialStatementType.BALANCE_SHEET,
            requested_ratio_family=None,
            expected_instrument_identity=_instrument(),
            expected_frequency=FinancialReportingFrequency.ANNUAL,
            expected_currency="CNY",
            expected_consolidation_scope=FinancialConsolidationScope.CONSOLIDATED,
            pit_as_of_date=date(2026, 7, 29),
        )

    ninety_percent = assess_without("accounts_payable")
    below_ninety = assess_without("accounts_payable", "accounts_receivable")
    missing_critical = assess_without("total_assets")

    assert ninety_percent.disposition is FinancialPeriodDisposition.ACCEPTED
    assert ninety_percent.core_coverage == Decimal("0.9")
    assert below_ninety.disposition is FinancialPeriodDisposition.REJECTED
    assert FinancialPeriodRejectionReason.INSUFFICIENT_COMPLETENESS in (
        below_ninety.rejection_reasons
    )
    assert missing_critical.critical_coverage < 1
    assert FinancialPeriodRejectionReason.INSUFFICIENT_COMPLETENESS in (
        missing_critical.rejection_reasons
    )


def test_ratio_family_rejects_more_than_ten_percent_missingness() -> None:
    declaration = financial_ratio_field_declaration(
        FinancialCompanyType.INDUSTRIAL_NON_BANK,
        FinancialRatioFamily.PROFIT,
    )
    all_fields = tuple(
        FinancialFieldValue(
            provider_field=field_name,
            original_value="1",
            original_unit=declaration.normalized_unit,
            normalized_field=field_name,
            normalized_value="1",
            normalized_unit=declaration.normalized_unit,
        )
        for field_name in declaration.normalized_fields
    )

    def assess_present(count: int):
        return assess_financial_period_candidate(
            _ratio_candidate(all_fields[:count]),
            requested_capability=FinancialCapability.RATIO_FAMILY,
            requested_statement_type=None,
            requested_ratio_family=FinancialRatioFamily.PROFIT,
            expected_instrument_identity=_instrument(),
            expected_frequency=FinancialReportingFrequency.QUARTERLY,
            expected_currency="CNY",
            expected_consolidation_scope=FinancialConsolidationScope.CONSOLIDATED,
            pit_as_of_date=date(2026, 7, 29),
        )

    ten_percent_missing = assess_present(9)
    twenty_percent_missing = assess_present(8)

    assert ten_percent_missing.disposition is FinancialPeriodDisposition.ACCEPTED
    assert ten_percent_missing.core_coverage == Decimal("0.9")
    assert twenty_percent_missing.disposition is FinancialPeriodDisposition.REJECTED
    assert FinancialPeriodRejectionReason.INSUFFICIENT_COMPLETENESS in (
        twenty_percent_missing.rejection_reasons
    )


def test_ratio_family_history_requires_eight_usable_reporting_periods() -> None:
    declaration = financial_ratio_field_declaration(
        FinancialCompanyType.INDUSTRIAL_NON_BANK,
        FinancialRatioFamily.PROFIT,
    )
    fields = tuple(
        FinancialFieldValue(
            provider_field=field_name,
            original_value="1",
            original_unit="RATIO",
            normalized_field=field_name,
            normalized_value="1",
            normalized_unit="RATIO",
        )
        for field_name in declaration.normalized_fields
    )
    periods = (
        date(2024, 3, 31),
        date(2024, 6, 30),
        date(2024, 9, 30),
        date(2024, 12, 31),
        date(2025, 3, 31),
        date(2025, 6, 30),
        date(2025, 9, 30),
        date(2025, 12, 31),
    )
    assessments = tuple(
        assess_financial_period_candidate(
            _ratio_candidate(fields, period_end=period),
            requested_capability=FinancialCapability.RATIO_FAMILY,
            requested_statement_type=None,
            requested_ratio_family=FinancialRatioFamily.PROFIT,
            expected_instrument_identity=_instrument(),
            expected_frequency=FinancialReportingFrequency.QUARTERLY,
            expected_currency="CNY",
            expected_consolidation_scope=FinancialConsolidationScope.CONSOLIDATED,
            pit_as_of_date=date(2026, 7, 29),
        )
        for period in periods
    )

    complete = assess_financial_ratio_history_coverage(
        instrument_identity=_instrument(),
        ratio_family=FinancialRatioFamily.PROFIT,
        company_type=FinancialCompanyType.INDUSTRIAL_NON_BANK,
        consolidation_scope=FinancialConsolidationScope.CONSOLIDATED,
        currency="CNY",
        assessments=assessments,
        eligible_reporting_period_ends=periods,
    )
    missing_one = assess_financial_ratio_history_coverage(
        instrument_identity=_instrument(),
        ratio_family=FinancialRatioFamily.PROFIT,
        company_type=FinancialCompanyType.INDUSTRIAL_NON_BANK,
        consolidation_scope=FinancialConsolidationScope.CONSOLIDATED,
        currency="CNY",
        assessments=assessments[:-1],
        eligible_reporting_period_ends=periods,
    )

    assert complete.complete is True
    assert len(complete.target_reporting_period_ends) == 8
    assert missing_one.complete is False
    assert missing_one.missing_reporting_period_ends == (periods[-1],)


def test_ratio_history_zero_eligible_periods_is_typed_incomplete() -> None:
    result = assess_financial_ratio_history_coverage(
        instrument_identity=_instrument(),
        ratio_family=FinancialRatioFamily.PROFIT,
        company_type=FinancialCompanyType.INDUSTRIAL_NON_BANK,
        consolidation_scope=FinancialConsolidationScope.CONSOLIDATED,
        currency="CNY",
        assessments=(),
        eligible_reporting_period_ends=(),
    )

    assert result.complete is False
    assert result.target_reporting_period_ends == ()
    assert result.rejection_reasons == (
        FinancialPeriodRejectionReason.INSUFFICIENT_COMPLETENESS,
    )


def test_missing_first_publication_is_current_only_but_missing_announcement_rejects() -> None:
    fields = _declared_statement_fields(FinancialCompanyType.BANK)

    def assess(candidate: FinancialPeriodCandidate):
        return assess_financial_period_candidate(
            candidate,
            requested_capability=FinancialCapability.STATEMENT,
            requested_statement_type=FinancialStatementType.BALANCE_SHEET,
            requested_ratio_family=None,
            expected_instrument_identity=_instrument(),
            expected_frequency=FinancialReportingFrequency.ANNUAL,
            expected_currency="CNY",
            expected_consolidation_scope=FinancialConsolidationScope.CONSOLIDATED,
            pit_as_of_date=date(2026, 7, 29),
        )

    current_only = assess(
        _candidate(FinancialCompanyType.BANK, fields, f_ann_date=None)
    )
    missing_announcement = assess(
        _candidate(FinancialCompanyType.BANK, fields, ann_date=None)
    )

    assert current_only.disposition is FinancialPeriodDisposition.CURRENT_ONLY
    assert current_only.usable_for_current_analysis is True
    assert current_only.strict_pit_eligible is False
    assert current_only.rejection_reasons == (
        FinancialPeriodRejectionReason.MISSING_FIRST_PUBLICATION_DATE,
    )
    assert missing_announcement.disposition is FinancialPeriodDisposition.REJECTED
    assert FinancialPeriodRejectionReason.INCOMPATIBLE_METADATA in (
        missing_announcement.rejection_reasons
    )


def test_strict_pit_requires_publication_and_observation_by_as_of_date() -> None:
    fields = _declared_statement_fields(FinancialCompanyType.BANK)
    future_publication = _candidate(
        FinancialCompanyType.BANK,
        fields,
        f_ann_date=date(2026, 7, 30),
    )
    future_observation = _candidate(
        FinancialCompanyType.BANK,
        fields,
        observed_at=datetime(2026, 7, 30, 12, 0, tzinfo=timezone.utc),
    )

    def assess(candidate: FinancialPeriodCandidate):
        return assess_financial_period_candidate(
            candidate,
            requested_capability=FinancialCapability.STATEMENT,
            requested_statement_type=FinancialStatementType.BALANCE_SHEET,
            requested_ratio_family=None,
            expected_instrument_identity=_instrument(),
            expected_frequency=FinancialReportingFrequency.ANNUAL,
            expected_currency="CNY",
            expected_consolidation_scope=(
                FinancialConsolidationScope.CONSOLIDATED
            ),
            pit_as_of_date=date(2026, 7, 29),
        )

    publication_assessment = assess(future_publication)
    observation_assessment = assess(future_observation)

    assert publication_assessment.disposition is FinancialPeriodDisposition.CURRENT_ONLY
    assert observation_assessment.disposition is FinancialPeriodDisposition.CURRENT_ONLY
    assert publication_assessment.strict_pit_eligible is False
    assert observation_assessment.strict_pit_eligible is False
    assert publication_assessment.pit_as_of_date == date(2026, 7, 29)
    assert publication_assessment.rejection_reasons == (
        FinancialPeriodRejectionReason.PIT_CUTOFF_NOT_SATISFIED,
    )


def test_completeness_assessment_rejects_forged_accepted_coverage() -> None:
    accepted = assess_financial_period_candidate(
        _candidate(
            FinancialCompanyType.BANK,
            _declared_statement_fields(FinancialCompanyType.BANK),
        ),
        requested_capability=FinancialCapability.STATEMENT,
        requested_statement_type=FinancialStatementType.BALANCE_SHEET,
        requested_ratio_family=None,
        expected_instrument_identity=_instrument(),
        expected_frequency=FinancialReportingFrequency.ANNUAL,
        expected_currency="CNY",
        expected_consolidation_scope=FinancialConsolidationScope.CONSOLIDATED,
        pit_as_of_date=date(2026, 7, 29),
    )

    with pytest.raises(ValidationError):
        type(accepted).model_validate(
            {
                **accepted.model_dump(mode="json"),
                "present_critical_fields": [],
                "missing_critical_fields": accepted.declared_critical_fields,
                "critical_coverage": "0",
            }
        )


def test_unit_currency_and_scope_mismatches_prevent_period_acceptance() -> None:
    fields = _declared_statement_fields(FinancialCompanyType.BANK)
    incompatible_unit_fields = (
        FinancialFieldValue(
            **{
                **fields[0].model_dump(mode="json"),
                "normalized_unit": "CNY_THOUSANDS",
            }
        ),
        *fields[1:],
    )

    def assess(candidate: FinancialPeriodCandidate):
        return assess_financial_period_candidate(
            candidate,
            requested_capability=FinancialCapability.STATEMENT,
            requested_statement_type=FinancialStatementType.BALANCE_SHEET,
            requested_ratio_family=None,
            expected_instrument_identity=_instrument(),
            expected_frequency=FinancialReportingFrequency.ANNUAL,
            expected_currency="CNY",
            expected_consolidation_scope=FinancialConsolidationScope.CONSOLIDATED,
            pit_as_of_date=date(2026, 7, 29),
        )

    incompatible_unit = assess(
        _candidate(FinancialCompanyType.BANK, incompatible_unit_fields)
    )
    incompatible_currency = assess(
        _candidate(FinancialCompanyType.BANK, fields, currency="USD")
    )
    incompatible_scope = assess(
        _candidate(
            FinancialCompanyType.BANK,
            fields,
            consolidation_scope=FinancialConsolidationScope.PARENT,
        )
    )

    assert FinancialPeriodRejectionReason.INCOMPATIBLE_UNIT in (
        incompatible_unit.rejection_reasons
    )
    assert FinancialPeriodRejectionReason.INCOMPATIBLE_CURRENCY in (
        incompatible_currency.rejection_reasons
    )
    assert FinancialPeriodRejectionReason.INCOMPATIBLE_CONSOLIDATION_SCOPE in (
        incompatible_scope.rejection_reasons
    )


def _statement_assessment(
    period_end: date,
    frequency: FinancialReportingFrequency,
):
    return assess_financial_period_candidate(
        _candidate(
            FinancialCompanyType.BANK,
            _declared_statement_fields(FinancialCompanyType.BANK),
            period_end=period_end,
            frequency=frequency,
        ),
        requested_capability=FinancialCapability.STATEMENT,
        requested_statement_type=FinancialStatementType.BALANCE_SHEET,
        requested_ratio_family=None,
        expected_instrument_identity=_instrument(),
        expected_frequency=frequency,
        expected_currency="CNY",
        expected_consolidation_scope=FinancialConsolidationScope.CONSOLIDATED,
        pit_as_of_date=date(2026, 7, 29),
    )


def test_normal_history_requires_latest_five_annual_and_eight_reporting_periods() -> None:
    eligible_annual = tuple(date(year, 12, 31) for year in range(2020, 2026))
    eligible_reporting = (
        date(2023, 3, 31),
        date(2023, 6, 30),
        date(2023, 9, 30),
        date(2023, 12, 31),
        date(2024, 3, 31),
        date(2024, 6, 30),
        date(2024, 9, 30),
        date(2024, 12, 31),
        date(2025, 3, 31),
        date(2025, 6, 30),
    )
    annual_assessments = tuple(
        _statement_assessment(period, FinancialReportingFrequency.ANNUAL)
        for period in eligible_annual[-5:]
    )
    reporting_assessments = tuple(
        _statement_assessment(period, FinancialReportingFrequency.QUARTERLY)
        for period in eligible_reporting[-8:]
    )

    complete = assess_financial_history_coverage(
        instrument_identity=_instrument(),
        statement_type=FinancialStatementType.BALANCE_SHEET,
        company_type=FinancialCompanyType.BANK,
        consolidation_scope=FinancialConsolidationScope.CONSOLIDATED,
        currency="CNY",
        assessments=(*annual_assessments, *reporting_assessments),
        eligible_annual_period_ends=eligible_annual,
        eligible_reporting_period_ends=eligible_reporting,
    )
    missing_one = assess_financial_history_coverage(
        instrument_identity=_instrument(),
        statement_type=FinancialStatementType.BALANCE_SHEET,
        company_type=FinancialCompanyType.BANK,
        consolidation_scope=FinancialConsolidationScope.CONSOLIDATED,
        currency="CNY",
        assessments=(*annual_assessments, *reporting_assessments[:-1]),
        eligible_annual_period_ends=eligible_annual,
        eligible_reporting_period_ends=eligible_reporting,
    )
    undersized_without_listing_provenance = assess_financial_history_coverage(
        instrument_identity=_instrument(),
        statement_type=FinancialStatementType.BALANCE_SHEET,
        company_type=FinancialCompanyType.BANK,
        consolidation_scope=FinancialConsolidationScope.CONSOLIDATED,
        currency="CNY",
        assessments=(
            _statement_assessment(
                date(2025, 12, 31),
                FinancialReportingFrequency.ANNUAL,
            ),
            _statement_assessment(
                date(2025, 12, 31),
                FinancialReportingFrequency.QUARTERLY,
            ),
        ),
        eligible_annual_period_ends=(date(2025, 12, 31),),
        eligible_reporting_period_ends=(date(2025, 12, 31),),
    )

    assert complete.complete is True
    assert len(complete.target_annual_period_ends) == 5
    assert len(complete.target_reporting_period_ends) == 8
    assert missing_one.complete is False
    assert missing_one.missing_reporting_period_ends == (eligible_reporting[-1],)
    assert undersized_without_listing_provenance.complete is False


def test_statement_history_binds_context_and_counts_annual_year_ends() -> None:
    annual_periods = tuple(date(year, 12, 31) for year in range(2021, 2026))
    quarterly_periods = (
        date(2025, 3, 31),
        date(2025, 6, 30),
        date(2025, 9, 30),
    )
    reporting_periods = (*annual_periods, *quarterly_periods)
    matching = (
        *(
            _statement_assessment(period, FinancialReportingFrequency.ANNUAL)
            for period in annual_periods
        ),
        *(
            _statement_assessment(period, FinancialReportingFrequency.QUARTERLY)
            for period in quarterly_periods
        ),
    )
    wrong_instrument = assess_financial_period_candidate(
        _candidate(
            FinancialCompanyType.BANK,
            _declared_statement_fields(FinancialCompanyType.BANK),
            period_end=quarterly_periods[-1],
            frequency=FinancialReportingFrequency.QUARTERLY,
            instrument_identity=_instrument(symbol="600000.SS"),
        ),
        requested_capability=FinancialCapability.STATEMENT,
        requested_statement_type=FinancialStatementType.BALANCE_SHEET,
        requested_ratio_family=None,
        expected_instrument_identity=_instrument(symbol="600000.SS"),
        expected_frequency=FinancialReportingFrequency.QUARTERLY,
        expected_currency="CNY",
        expected_consolidation_scope=FinancialConsolidationScope.CONSOLIDATED,
        pit_as_of_date=date(2026, 7, 29),
    )
    ratio_declaration = financial_ratio_field_declaration(
        FinancialCompanyType.INDUSTRIAL_NON_BANK,
        FinancialRatioFamily.PROFIT,
    )
    ratio_fields = tuple(
        FinancialFieldValue(
            provider_field=field_name,
            original_value="1",
            original_unit="RATIO",
            normalized_field=field_name,
            normalized_value="1",
            normalized_unit="RATIO",
        )
        for field_name in ratio_declaration.normalized_fields
    )
    wrong_capability = assess_financial_period_candidate(
        _ratio_candidate(ratio_fields, period_end=quarterly_periods[-1]),
        requested_capability=FinancialCapability.RATIO_FAMILY,
        requested_statement_type=None,
        requested_ratio_family=FinancialRatioFamily.PROFIT,
        expected_instrument_identity=_instrument(),
        expected_frequency=FinancialReportingFrequency.QUARTERLY,
        expected_currency="CNY",
        expected_consolidation_scope=FinancialConsolidationScope.CONSOLIDATED,
        pit_as_of_date=date(2026, 7, 29),
    )

    complete = assess_financial_history_coverage(
        instrument_identity=_instrument(),
        statement_type=FinancialStatementType.BALANCE_SHEET,
        company_type=FinancialCompanyType.BANK,
        consolidation_scope=FinancialConsolidationScope.CONSOLIDATED,
        currency="CNY",
        assessments=(*matching, wrong_instrument),
        eligible_annual_period_ends=annual_periods,
        eligible_reporting_period_ends=reporting_periods,
    )
    missing_without_matching = assess_financial_history_coverage(
        instrument_identity=_instrument(),
        statement_type=FinancialStatementType.BALANCE_SHEET,
        company_type=FinancialCompanyType.BANK,
        consolidation_scope=FinancialConsolidationScope.CONSOLIDATED,
        currency="CNY",
        assessments=(*matching[:-1], wrong_instrument, wrong_capability),
        eligible_annual_period_ends=annual_periods,
        eligible_reporting_period_ends=reporting_periods,
    )

    assert complete.complete is True
    assert set(annual_periods) <= set(complete.covered_reporting_period_ends)
    assert missing_without_matching.complete is False
    assert missing_without_matching.missing_reporting_period_ends == (
        quarterly_periods[-1],
    )


def test_since_listing_exception_requires_every_post_listing_period() -> None:
    listing_date = date(2024, 6, 15)
    listing_provenance = FinancialListingProvenance(
        provider_id="fixture_registry",
        source_ref="acq.v1:listing:" + "3" * 64,
        observed_at=_OBSERVED_AT,
    )
    eligible_annual = tuple(date(year, 12, 31) for year in range(2022, 2026))
    eligible_reporting = (
        date(2023, 12, 31),
        date(2024, 3, 31),
        date(2024, 6, 30),
        date(2024, 9, 30),
        date(2024, 12, 31),
        date(2025, 3, 31),
        date(2025, 6, 30),
        date(2025, 9, 30),
        date(2025, 12, 31),
    )
    expected_annual = tuple(period for period in eligible_annual if period >= listing_date)
    expected_reporting = tuple(
        period for period in eligible_reporting if period >= listing_date
    )
    post_listing = (
        *(
            _statement_assessment(period, FinancialReportingFrequency.ANNUAL)
            for period in expected_annual
        ),
        *(
            _statement_assessment(period, FinancialReportingFrequency.QUARTERLY)
            for period in expected_reporting
        ),
    )
    pre_listing = _statement_assessment(
        date(2024, 3, 31),
        FinancialReportingFrequency.QUARTERLY,
    )

    complete = assess_financial_history_coverage(
        instrument_identity=_instrument(),
        statement_type=FinancialStatementType.BALANCE_SHEET,
        company_type=FinancialCompanyType.BANK,
        consolidation_scope=FinancialConsolidationScope.CONSOLIDATED,
        currency="CNY",
        assessments=(*post_listing, pre_listing),
        eligible_annual_period_ends=eligible_annual,
        eligible_reporting_period_ends=eligible_reporting,
        listing_date=listing_date,
        listing_provenance=listing_provenance,
    )
    missing_post_listing = assess_financial_history_coverage(
        instrument_identity=_instrument(),
        statement_type=FinancialStatementType.BALANCE_SHEET,
        company_type=FinancialCompanyType.BANK,
        consolidation_scope=FinancialConsolidationScope.CONSOLIDATED,
        currency="CNY",
        assessments=(
            *(
                assessment
                for assessment in post_listing
                if assessment.period_end != date(2025, 9, 30)
            ),
            pre_listing,
        ),
        eligible_annual_period_ends=eligible_annual,
        eligible_reporting_period_ends=eligible_reporting,
        listing_date=listing_date,
        listing_provenance=listing_provenance,
    )

    assert complete.complete is True
    assert complete.since_listing_exception is not None
    assert complete.since_listing_exception.supported is True
    assert complete.since_listing_exception.ignored_pre_listing_period_ends == (
        date(2024, 3, 31),
    )
    assert missing_post_listing.complete is False
    assert missing_post_listing.since_listing_exception is not None
    assert missing_post_listing.since_listing_exception.missing_reporting_period_ends == (
        date(2025, 9, 30),
    )
    with pytest.raises(ValidationError):
        FinancialSinceListingException.model_validate(
            {
                **complete.since_listing_exception.model_dump(mode="json"),
                "covered_reporting_period_ends": [],
            }
        )


def test_manifest_retains_rejected_artifacts_and_excludes_raw_failures() -> None:
    accepted_candidate = _candidate(
        FinancialCompanyType.BANK,
        _declared_statement_fields(FinancialCompanyType.BANK),
        artifact_marker="accepted",
    )
    rejected_candidate = _candidate(
        FinancialCompanyType.UNKNOWN,
        (),
        artifact_marker="rejected",
    )
    accepted_assessment = assess_financial_period_candidate(
        accepted_candidate,
        requested_capability=FinancialCapability.STATEMENT,
        requested_statement_type=FinancialStatementType.BALANCE_SHEET,
        requested_ratio_family=None,
        expected_instrument_identity=_instrument(),
        expected_frequency=FinancialReportingFrequency.ANNUAL,
        expected_currency="CNY",
        expected_consolidation_scope=FinancialConsolidationScope.CONSOLIDATED,
        pit_as_of_date=date(2026, 7, 29),
    )
    rejected_assessment = assess_financial_period_candidate(
        rejected_candidate,
        requested_capability=FinancialCapability.STATEMENT,
        requested_statement_type=FinancialStatementType.BALANCE_SHEET,
        requested_ratio_family=None,
        expected_instrument_identity=_instrument(),
        expected_frequency=FinancialReportingFrequency.ANNUAL,
        expected_currency="CNY",
        expected_consolidation_scope=FinancialConsolidationScope.CONSOLIDATED,
        pit_as_of_date=date(2026, 7, 29),
    )
    aggregate = assess_financial_history_coverage(
        instrument_identity=_instrument(),
        statement_type=FinancialStatementType.BALANCE_SHEET,
        company_type=FinancialCompanyType.BANK,
        consolidation_scope=FinancialConsolidationScope.CONSOLIDATED,
        currency="CNY",
        assessments=(accepted_assessment,),
        eligible_annual_period_ends=(date(2025, 12, 31),),
        eligible_reporting_period_ends=(date(2025, 12, 31),),
    )
    artifacts = (
        FinancialProviderArtifactEntry(artifact=accepted_candidate.artifact),
        FinancialProviderArtifactEntry(artifact=rejected_candidate.artifact),
    )
    candidates = (
        FinancialProviderCandidate(
            candidate=accepted_candidate,
            assessment=accepted_assessment,
        ),
        FinancialProviderCandidate(
            candidate=rejected_candidate,
            assessment=rejected_assessment,
        ),
    )
    outcomes = (
        FinancialAcquisitionOutcome.available(
            provider_id="fixture_provider",
            capability=FinancialCapability.STATEMENT,
            artifact_identity=accepted_candidate.artifact.artifact_identity,
        ),
        FinancialAcquisitionOutcome.available(
            provider_id="fixture_provider",
            capability=FinancialCapability.STATEMENT,
            artifact_identity=rejected_candidate.artifact.artifact_identity,
        ),
        FinancialAcquisitionOutcome.unavailable(
            provider_id="fallback_provider",
            capability=FinancialCapability.STATEMENT,
            reason=FinancialPeriodRejectionReason.PERMISSION_DENIED,
        ),
    )
    selection = FinancialPeriodSelection.create(
        candidate=accepted_candidate,
        assessment=accepted_assessment,
    )
    rejected = FinancialRejectedPeriod.create(
        candidate=rejected_candidate,
        assessment=rejected_assessment,
    )

    first = FinancialAcquisitionManifest.create(
        capability_routing_plan_signature=(
            "mainland-routing-plan:v1:" + "4" * 64
        ),
        instrument_identity=_instrument(),
        requested_capability=FinancialCapability.STATEMENT,
        requested_statement_type=FinancialStatementType.BALANCE_SHEET,
        requested_ratio_family=None,
        artifacts=artifacts,
        acquisition_outcomes=outcomes,
        provider_candidates=candidates,
        selections=(selection,),
        rejected_periods=(rejected,),
        overlaps=(),
        conflicts=(),
        aggregate_completeness=aggregate,
    )
    reordered = FinancialAcquisitionManifest.create(
        capability_routing_plan_signature=(
            "mainland-routing-plan:v1:" + "4" * 64
        ),
        instrument_identity=_instrument(),
        requested_capability=FinancialCapability.STATEMENT,
        requested_statement_type=FinancialStatementType.BALANCE_SHEET,
        requested_ratio_family=None,
        artifacts=tuple(reversed(artifacts)),
        acquisition_outcomes=tuple(reversed(outcomes)),
        provider_candidates=tuple(reversed(candidates)),
        selections=(selection,),
        rejected_periods=(rejected,),
        overlaps=(),
        conflicts=(),
        aggregate_completeness=aggregate,
    )

    assert first == reordered
    assert first.manifest_identity.startswith("financial-manifest:v1:")
    assert rejected.artifact_identity in {
        entry.artifact.artifact_identity for entry in first.artifacts
    }
    serialized = first.model_dump_json().casefold()
    assert "fixture-secret" not in serialized
    assert "traceback" not in serialized
    assert "raw_error" not in serialized
    assert "source_facts" not in serialized
    with pytest.raises(ValidationError):
        FinancialAcquisitionOutcome.model_validate(
            {
                **outcomes[-1].model_dump(mode="json"),
                "raw_error": "traceback token=fixture-secret",
            }
        )

    unavailable_reasons = (
        AcquisitionUnavailableReason.AUTHENTICATION,
        AcquisitionUnavailableReason.RATE_LIMITED,
        AcquisitionUnavailableReason.EMPTY_FRAME,
        AcquisitionUnavailableReason.NO_DATA,
        AcquisitionUnavailableReason.MALFORMED_RESPONSE,
        AcquisitionUnavailableReason.USAGE_NOT_ENTITLED,
        AcquisitionUnavailableReason.TIMEOUT,
        AcquisitionUnavailableReason.PROVIDER_ERROR,
    )
    typed_outcomes = tuple(
        FinancialAcquisitionOutcome.unavailable(
            provider_id="fallback_provider",
            capability=FinancialCapability.STATEMENT,
            reason=reason,
        )
        for reason in unavailable_reasons
    )
    assert tuple(outcome.reason for outcome in typed_outcomes) == unavailable_reasons

    unrelated_ratio_completeness = assess_financial_ratio_history_coverage(
        instrument_identity=_instrument(),
        ratio_family=FinancialRatioFamily.PROFIT,
        company_type=FinancialCompanyType.BANK,
        consolidation_scope=FinancialConsolidationScope.CONSOLIDATED,
        currency="CNY",
        assessments=(),
        eligible_reporting_period_ends=(),
    )
    with pytest.raises(ValidationError):
        FinancialAcquisitionManifest.model_validate(
            {
                **first.model_dump(mode="json"),
                "aggregate_completeness": unrelated_ratio_completeness.model_dump(
                    mode="json"
                ),
            }
        )


def test_critical_conflict_marks_only_the_overlapping_period_conflicted() -> None:
    selected_fields = _declared_statement_fields(FinancialCompanyType.BANK)
    conflicting_fields = tuple(
        FinancialFieldValue(
            **{
                **field.model_dump(mode="json"),
                "original_value": "2",
                "normalized_value": "2",
            }
        )
        if field.normalized_field == "customer_deposits"
        else field
        for field in selected_fields
    )
    selected = _candidate(
        FinancialCompanyType.BANK,
        selected_fields,
        artifact_marker="selected-revision",
    )
    overlapping = _candidate(
        FinancialCompanyType.BANK,
        conflicting_fields,
        artifact_marker="conflicting-revision",
    )
    unrelated = _candidate(
        FinancialCompanyType.BANK,
        selected_fields,
        artifact_marker="unrelated-period",
        period_end=date(2024, 12, 31),
    )

    def assess(candidate: FinancialPeriodCandidate):
        return assess_financial_period_candidate(
            candidate,
            requested_capability=FinancialCapability.STATEMENT,
            requested_statement_type=FinancialStatementType.BALANCE_SHEET,
            requested_ratio_family=None,
            expected_instrument_identity=_instrument(),
            expected_frequency=candidate.period_identity.frequency,
            expected_currency="CNY",
            expected_consolidation_scope=FinancialConsolidationScope.CONSOLIDATED,
            pit_as_of_date=date(2026, 7, 29),
        )

    selected_assessment = assess(selected)
    overlapping_assessment = assess(overlapping)
    unrelated_assessment = assess(unrelated)
    overlap, conflicts = compare_financial_period_candidates(selected, overlapping)
    conflicted = mark_financial_period_conflicted(overlapping_assessment)

    assert overlap.disposition == "critical_conflict"
    assert tuple(conflict.normalized_field for conflict in conflicts) == (
        "customer_deposits",
    )
    assert selected_assessment.disposition is FinancialPeriodDisposition.ACCEPTED
    assert conflicted.disposition is FinancialPeriodDisposition.CONFLICTED
    assert conflicted.usable_for_current_analysis is False
    assert FinancialPeriodRejectionReason.CONFLICTING_CRITICAL_VALUES in (
        conflicted.rejection_reasons
    )
    assert unrelated_assessment.disposition is FinancialPeriodDisposition.ACCEPTED


def test_cross_provider_company_type_contradiction_is_typed_and_conflicted() -> None:
    bank = _candidate(
        FinancialCompanyType.BANK,
        _declared_statement_fields(FinancialCompanyType.BANK),
        artifact_marker="bank-classification",
    )
    industrial = _candidate(
        FinancialCompanyType.INDUSTRIAL_NON_BANK,
        _declared_statement_fields(FinancialCompanyType.INDUSTRIAL_NON_BANK),
        artifact_marker="industrial-classification",
    )
    industrial_assessment = assess_financial_period_candidate(
        industrial,
        requested_capability=FinancialCapability.STATEMENT,
        requested_statement_type=FinancialStatementType.BALANCE_SHEET,
        requested_ratio_family=None,
        expected_instrument_identity=_instrument(),
        expected_frequency=FinancialReportingFrequency.ANNUAL,
        expected_currency="CNY",
        expected_consolidation_scope=FinancialConsolidationScope.CONSOLIDATED,
        pit_as_of_date=date(2026, 7, 29),
    )

    overlap, conflicts = compare_financial_period_candidates(bank, industrial)
    conflicted = mark_financial_period_company_type_conflicted(
        industrial_assessment
    )

    assert conflicts == ()
    assert overlap.disposition == "company_type_conflict"
    assert overlap.rejection_reasons == (
        FinancialPeriodRejectionReason.AMBIGUOUS_COMPANY_TYPE,
    )
    assert conflicted.disposition is FinancialPeriodDisposition.CONFLICTED
    assert conflicted.rejection_reasons == (
        FinancialPeriodRejectionReason.AMBIGUOUS_COMPANY_TYPE,
    )

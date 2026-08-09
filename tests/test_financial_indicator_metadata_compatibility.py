from __future__ import annotations

import json
from datetime import date, datetime, timezone
from hashlib import sha256

import pytest
from pydantic import ValidationError

from tradingagents.dataflows.financial_contracts import (
    FINANCIAL_RATIO_DECLARATION_V1,
    FinancialCapability,
    FinancialCompanyType,
    FinancialConsolidationScope,
    FinancialFieldValue,
    FinancialFilingMetadata,
    FinancialPeriodCandidate,
    FinancialPeriodIdentity,
    FinancialProviderArtifactIdentity,
    FinancialProviderDatasetIdentity,
    FinancialRatioFamily,
    FinancialReportingFrequency,
    resolve_financial_company_type,
)
from tradingagents.dataflows.financial_indicator_metadata import (
    FINANCIAL_INDICATOR_METADATA_COMPATIBILITY_VERSION,
    FinancialIndicatorMetadataDimension,
    FinancialIndicatorMetadataEvidenceOrigin,
    FinancialIndicatorMetadataReason,
    FinancialIndicatorProviderFieldMetadata,
    FinancialIndicatorSemanticAssessmentState,
    assess_financial_indicator_metadata_compatibility,
)
from tradingagents.evidence import (
    IdentityProvenance,
    InstrumentIdentityEvidence,
    InstrumentKind,
)

_OBSERVED_AT = datetime(2026, 5, 1, tzinfo=timezone.utc)


def _instrument() -> InstrumentIdentityEvidence:
    return InstrumentIdentityEvidence(
        symbol="601328.SS",
        venue="XSHG",
        instrument_kind=InstrumentKind.EQUITY,
        currency="CNY",
        provenance=IdentityProvenance(
            provider="fixture-registry",
            source_ref="acq.v1:identity:" + "1" * 64,
            retrieved_at="2026-05-01T00:00:00Z",
            artifact_sha256="1" * 64,
        ),
    )


def _candidate(
    fields: tuple[FinancialFieldValue, ...],
    *,
    family: FinancialRatioFamily = FinancialRatioFamily.PROFIT,
    f_ann_date: date | None = None,
    raw_currency: str = "XXX",
    raw_scope: FinancialConsolidationScope = FinancialConsolidationScope.UNKNOWN,
    filing_observed_at: datetime = _OBSERVED_AT,
) -> FinancialPeriodCandidate:
    instrument = _instrument()
    dataset = FinancialProviderDatasetIdentity.create(
        provider_id="tushare",
        endpoint_id="fina_indicator",
        dataset_id="fina_indicator_history",
        schema_identity="tushare-fina-indicator-schema-v1",
    )
    artifact = FinancialProviderArtifactIdentity.create(
        dataset=dataset,
        canonical_request={"symbol": instrument.symbol},
        provider_metadata={"endpoint_id": "fina_indicator"},
        retrieved_at=_OBSERVED_AT,
        observed_at=_OBSERVED_AT,
        payload={"fixture": "indicator"},
    )
    period_identity = FinancialPeriodIdentity.create(
        instrument_identity=instrument,
        capability=FinancialCapability.RATIO_FAMILY,
        statement_type=None,
        ratio_family=family,
        period_end=date(2025, 12, 31),
        frequency=FinancialReportingFrequency.ANNUAL,
        company_type=FinancialCompanyType.BANK,
        consolidation_scope=raw_scope,
        currency=raw_currency,
        provider_revision_binding=artifact.artifact_identity,
    )
    return FinancialPeriodCandidate.create(
        artifact=artifact,
        period_identity=period_identity,
        filing_metadata=FinancialFilingMetadata(
            ann_date=date(2026, 4, 20),
            f_ann_date=f_ann_date,
            report_type=None,
            comp_type=None,
            update_flag="1",
            retrieved_at=_OBSERVED_AT,
            observed_at=filing_observed_at,
            local_provider_revision_identity=artifact.artifact_identity,
        ),
        company_type_resolution=resolve_financial_company_type(
            provider_declared_type=FinancialCompanyType.BANK,
            provider_declaration_qualified=True,
            classifier_metadata={},
            present_fields=tuple(field.normalized_field for field in fields),
        ),
        fields=fields,
    )


def _field(
    normalized_field: str,
    *,
    original_unit: str,
    normalized_unit: str,
) -> FinancialFieldValue:
    return FinancialFieldValue(
        provider_field=normalized_field,
        original_value="12.5",
        original_unit=original_unit,
        normalized_field=normalized_field,
        normalized_value="0.125",
        normalized_unit=normalized_unit,
    )


def _metadata(
    candidate: FinancialPeriodCandidate,
    normalized_field: str,
    *,
    value_currency: str | None = None,
    value_currency_marker: str | None = "XXX",
    scope: FinancialConsolidationScope | None = None,
    scope_marker: str | None = "UNKNOWN",
) -> FinancialIndicatorProviderFieldMetadata:
    return FinancialIndicatorProviderFieldMetadata(
        provider_id=candidate.artifact.dataset.provider_id,
        provider_artifact_identity=candidate.artifact.artifact_identity,
        normalized_field=normalized_field,
        unit_normalization_contract_version="fixture-indicator-normalizer-v1",
        value_currency=value_currency,
        raw_value_currency_marker=value_currency_marker,
        filing_consolidation_scope=scope,
        raw_filing_consolidation_scope_marker=scope_marker,
    )


def _assess(
    candidate: FinancialPeriodCandidate,
    metadata: tuple[FinancialIndicatorProviderFieldMetadata, ...],
    *,
    pit_as_of_date: date = date(2026, 5, 1),
):
    assert candidate.period_identity.ratio_family is not None
    return assess_financial_indicator_metadata_compatibility(
        candidate=candidate,
        family=candidate.period_identity.ratio_family,
        instrument_identity=_instrument(),
        requested_value_currency="CNY",
        requested_filing_consolidation_scope=(
            FinancialConsolidationScope.CONSOLIDATED
        ),
        pit_as_of_date=pit_as_of_date,
        declaration_version=FINANCIAL_RATIO_DECLARATION_V1,
        provider_field_metadata=metadata,
    )


def _dimension(assessment, dimension: FinancialIndicatorMetadataDimension):
    field = assessment.field_assessments[0]
    return next(item for item in field.dimensions if item.dimension is dimension)


def _resign_assessment_payload(payload: dict[str, object]) -> None:
    material = dict(payload)
    material.pop("assessment_identity", None)
    digest = sha256(
        json.dumps(
            material,
            allow_nan=False,
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        ).encode("utf-8")
    ).hexdigest()
    payload["assessment_identity"] = (
        "financial-indicator-metadata-compatibility:v1:" + digest
    )


def test_raw_unknown_is_distinct_from_closed_semantic_assessment_states() -> None:
    assert {item.name for item in FinancialIndicatorSemanticAssessmentState} == {
        "PROVEN",
        "UNPROVEN",
        "NOT_APPLICABLE",
    }
    assert "UNKNOWN" not in {
        item.name for item in FinancialIndicatorSemanticAssessmentState
    }

    candidate = _candidate(
        (_field("gross_margin", original_unit="PERCENT", normalized_unit="RATIO"),)
    )
    assessment = _assess(candidate, (_metadata(candidate, "gross_margin"),))
    scope = _dimension(
        assessment,
        FinancialIndicatorMetadataDimension.FILING_CONSOLIDATION_SCOPE,
    )

    assert scope.raw_provider_marker == "UNKNOWN"
    assert scope.state is FinancialIndicatorSemanticAssessmentState.UNPROVEN
    assert scope.state is not FinancialIndicatorSemanticAssessmentState.NOT_APPLICABLE
    assert scope.reason is FinancialIndicatorMetadataReason.MATERIAL_EVIDENCE_UNPROVEN


def test_tushare_ratio_keeps_instrument_and_value_currency_separate() -> None:
    candidate = _candidate(
        (_field("gross_margin", original_unit="PERCENT", normalized_unit="RATIO"),)
    )
    assessment = _assess(candidate, (_metadata(candidate, "gross_margin"),))
    instrument_currency = _dimension(
        assessment,
        FinancialIndicatorMetadataDimension.INSTRUMENT_CURRENCY,
    )
    value_currency = _dimension(
        assessment,
        FinancialIndicatorMetadataDimension.VALUE_CURRENCY,
    )

    assert assessment.contract_version == FINANCIAL_INDICATOR_METADATA_COMPATIBILITY_VERSION
    assert instrument_currency.state is FinancialIndicatorSemanticAssessmentState.PROVEN
    assert instrument_currency.evidence_value == "CNY"
    assert instrument_currency.safe_evidence_origin is FinancialIndicatorMetadataEvidenceOrigin.AUTHORITATIVE_INSTRUMENT_IDENTITY
    assert value_currency.raw_provider_marker == "XXX"
    assert value_currency.state is FinancialIndicatorSemanticAssessmentState.NOT_APPLICABLE
    assert value_currency.evidence_value is None
    assert assessment.compatible is False


def test_request_intent_never_supplies_material_provider_currency_or_scope() -> None:
    candidate = _candidate(
        (
            _field(
                "earnings_per_share",
                original_unit="CURRENCY_PER_SHARE",
                normalized_unit="CURRENCY_PER_SHARE",
            ),
        )
    )
    assessment = _assess(
        candidate,
        (
            _metadata(
                candidate,
                "earnings_per_share",
                value_currency=None,
                value_currency_marker="UNKNOWN",
            ),
        ),
    )

    currency = _dimension(
        assessment,
        FinancialIndicatorMetadataDimension.VALUE_CURRENCY,
    )
    scope = _dimension(
        assessment,
        FinancialIndicatorMetadataDimension.FILING_CONSOLIDATION_SCOPE,
    )
    assert currency.expected_semantic_value == "CNY"
    assert currency.evidence_value is None
    assert currency.state is FinancialIndicatorSemanticAssessmentState.UNPROVEN
    assert scope.expected_semantic_value == "consolidated"
    assert scope.evidence_value is None
    assert scope.state is FinancialIndicatorSemanticAssessmentState.UNPROVEN


def test_proven_per_share_metadata_is_compatible_but_missing_f_ann_date_is_not_strict_pit() -> None:
    candidate = _candidate(
        (
            _field(
                "earnings_per_share",
                original_unit="CURRENCY_PER_SHARE",
                normalized_unit="CURRENCY_PER_SHARE",
            ),
        ),
        f_ann_date=None,
    )
    assessment = _assess(
        candidate,
        (
            _metadata(
                candidate,
                "earnings_per_share",
                value_currency="CNY",
                value_currency_marker="CNY",
                scope=FinancialConsolidationScope.CONSOLIDATED,
                scope_marker="CONSOLIDATED",
            ),
        ),
    )

    assert assessment.compatible is True
    assert assessment.current_only_metadata_eligible is True
    assert assessment.strict_pit_metadata_eligible is False
    assert assessment.first_publication_date is None


def test_complete_normalized_values_do_not_prove_material_scope() -> None:
    candidate = _candidate(
        (
            _field("gross_margin", original_unit="PERCENT", normalized_unit="RATIO"),
            _field("net_margin", original_unit="PERCENT", normalized_unit="RATIO"),
        )
    )
    assessment = _assess(
        candidate,
        (_metadata(candidate, "net_margin"), _metadata(candidate, "gross_margin")),
    )

    assert tuple(item.normalized_field for item in assessment.field_assessments) == (
        "gross_margin",
        "net_margin",
    )
    assert all(field.metadata_compatible is False for field in assessment.field_assessments)
    assert assessment.compatible is False
    assert assessment.current_only_metadata_eligible is False


def test_compatibility_identity_is_stable_to_input_order_and_changes_with_proof() -> None:
    candidate = _candidate(
        (
            _field("gross_margin", original_unit="PERCENT", normalized_unit="RATIO"),
            _field("net_margin", original_unit="PERCENT", normalized_unit="RATIO"),
        )
    )
    unknown = _assess(
        candidate,
        (_metadata(candidate, "net_margin"), _metadata(candidate, "gross_margin")),
    )
    reordered = _assess(
        candidate,
        (_metadata(candidate, "gross_margin"), _metadata(candidate, "net_margin")),
    )
    proven = _assess(
        candidate,
        tuple(
            _metadata(
                candidate,
                field,
                scope=FinancialConsolidationScope.CONSOLIDATED,
                scope_marker="CONSOLIDATED",
            )
            for field in ("gross_margin", "net_margin")
        ),
    )

    assert unknown == reordered
    assert unknown.assessment_identity.startswith(
        "financial-indicator-metadata-compatibility:v1:"
    )
    assert proven.assessment_identity != unknown.assessment_identity
    assert proven.compatible is True


def test_unknown_can_never_validate_as_proven() -> None:
    candidate = _candidate(
        (_field("gross_margin", original_unit="PERCENT", normalized_unit="RATIO"),)
    )
    assessment = _assess(candidate, (_metadata(candidate, "gross_margin"),))
    payload = assessment.model_dump(mode="json")
    payload["field_assessments"][0]["dimensions"][3]["state"] = "proven"
    payload["field_assessments"][0]["dimensions"][3]["evidence_value"] = "UNKNOWN"

    with pytest.raises(ValidationError, match="UNKNOWN.*PROVEN|identity mismatch"):
        type(assessment).model_validate(payload)


def test_unknown_marker_prevents_explicit_value_from_becoming_proof() -> None:
    candidate = _candidate(
        (
            _field(
                "earnings_per_share",
                original_unit="CURRENCY_PER_SHARE",
                normalized_unit="CURRENCY_PER_SHARE",
            ),
        )
    )
    assessment = _assess(
        candidate,
        (
            _metadata(
                candidate,
                "earnings_per_share",
                value_currency="CNY",
                value_currency_marker="UNKNOWN",
                scope=FinancialConsolidationScope.CONSOLIDATED,
                scope_marker="UNKNOWN",
            ),
        ),
    )

    assert _dimension(
        assessment,
        FinancialIndicatorMetadataDimension.VALUE_CURRENCY,
    ).state is FinancialIndicatorSemanticAssessmentState.UNPROVEN
    assert _dimension(
        assessment,
        FinancialIndicatorMetadataDimension.FILING_CONSOLIDATION_SCOPE,
    ).state is FinancialIndicatorSemanticAssessmentState.UNPROVEN
    assert assessment.compatible is False


def test_provider_metadata_must_bind_the_exact_candidate_artifact() -> None:
    candidate = _candidate(
        (_field("gross_margin", original_unit="PERCENT", normalized_unit="RATIO"),)
    )
    wrong_artifact = _metadata(candidate, "gross_margin").model_copy(
        update={
            "provider_artifact_identity": (
                "financial-provider-artifact:v1:" + "0" * 64
            )
        }
    )

    with pytest.raises(ValueError, match="artifact.*binding|provider metadata"):
        _assess(candidate, (wrong_artifact,))


def test_ratio_unit_requires_a_valid_original_basis_and_normalizer_lineage() -> None:
    candidate = _candidate(
        (_field("gross_margin", original_unit="CNY", normalized_unit="RATIO"),)
    )
    assessment = _assess(
        candidate,
        (
            _metadata(
                candidate,
                "gross_margin",
                scope=FinancialConsolidationScope.CONSOLIDATED,
                scope_marker="CONSOLIDATED",
            ),
        ),
    )

    unit = _dimension(assessment, FinancialIndicatorMetadataDimension.VALUE_UNIT)
    assert unit.state is FinancialIndicatorSemanticAssessmentState.UNPROVEN
    assert unit.reason is FinancialIndicatorMetadataReason.NORMALIZED_UNIT_BASIS_UNPROVEN
    assert assessment.compatible is False


def test_strict_pit_uses_identity_bound_artifact_observation_time() -> None:
    candidate = _candidate(
        (_field("gross_margin", original_unit="PERCENT", normalized_unit="RATIO"),),
        f_ann_date=date(2026, 4, 22),
        filing_observed_at=datetime(2026, 4, 22, tzinfo=timezone.utc),
    )
    assessment = _assess(
        candidate,
        (
            _metadata(
                candidate,
                "gross_margin",
                scope=FinancialConsolidationScope.CONSOLIDATED,
                scope_marker="CONSOLIDATED",
            ),
        ),
        pit_as_of_date=date(2026, 4, 25),
    )

    assert assessment.compatible is True
    assert assessment.current_only_metadata_eligible is True
    assert assessment.revision_observed_at == _OBSERVED_AT
    assert assessment.strict_pit_metadata_eligible is False


def test_strict_pit_also_preserves_filing_metadata_observation_cutoff() -> None:
    candidate = _candidate(
        (_field("gross_margin", original_unit="PERCENT", normalized_unit="RATIO"),),
        f_ann_date=date(2026, 4, 22),
        filing_observed_at=datetime(2026, 5, 5, tzinfo=timezone.utc),
    )
    assessment = _assess(
        candidate,
        (
            _metadata(
                candidate,
                "gross_margin",
                scope=FinancialConsolidationScope.CONSOLIDATED,
                scope_marker="CONSOLIDATED",
            ),
        ),
        pit_as_of_date=date(2026, 5, 2),
    )

    assert assessment.compatible is True
    assert assessment.current_only_metadata_eligible is True
    assert assessment.filing_metadata_observed_at == datetime(
        2026,
        5,
        5,
        tzinfo=timezone.utc,
    )
    assert assessment.strict_pit_metadata_eligible is False


def test_contradictory_canonical_raw_markers_remain_unproven() -> None:
    candidate = _candidate(
        (
            _field(
                "earnings_per_share",
                original_unit="CURRENCY_PER_SHARE",
                normalized_unit="CURRENCY_PER_SHARE",
            ),
        )
    )
    assessment = _assess(
        candidate,
        (
            _metadata(
                candidate,
                "earnings_per_share",
                value_currency="CNY",
                value_currency_marker="USD",
                scope=FinancialConsolidationScope.CONSOLIDATED,
                scope_marker="PARENT",
            ),
        ),
    )

    assert _dimension(
        assessment,
        FinancialIndicatorMetadataDimension.VALUE_CURRENCY,
    ).state is FinancialIndicatorSemanticAssessmentState.UNPROVEN
    assert _dimension(
        assessment,
        FinancialIndicatorMetadataDimension.FILING_CONSOLIDATION_SCOPE,
    ).state is FinancialIndicatorSemanticAssessmentState.UNPROVEN
    assert assessment.compatible is False


@pytest.mark.parametrize(
    ("dimension_index", "field", "value"),
    [
        (2, "raw_provider_marker", "USD"),
        (3, "raw_provider_marker", "PARENT"),
        (2, "safe_evidence_origin", "field_semantics_policy"),
        (2, "reason", "authoritative_instrument_identity_proven"),
    ],
)
def test_serialized_proven_assessment_rejects_contradiction_after_resigning(
    dimension_index: int,
    field: str,
    value: str,
) -> None:
    candidate = _candidate(
        (
            _field(
                "earnings_per_share",
                original_unit="CURRENCY_PER_SHARE",
                normalized_unit="CURRENCY_PER_SHARE",
            ),
        ),
        f_ann_date=date(2026, 4, 22),
    )
    assessment = _assess(
        candidate,
        (
            _metadata(
                candidate,
                "earnings_per_share",
                value_currency="CNY",
                value_currency_marker="CNY",
                scope=FinancialConsolidationScope.CONSOLIDATED,
                scope_marker="CONSOLIDATED",
            ),
        ),
    )
    payload = assessment.model_dump(mode="json")
    payload["field_assessments"][0]["dimensions"][dimension_index][field] = value
    _resign_assessment_payload(payload)

    with pytest.raises(ValidationError):
        type(assessment).model_validate(payload)


def test_companion_filing_matching_keys_cannot_enter_provider_metadata() -> None:
    with pytest.raises(ValidationError):
        FinancialIndicatorProviderFieldMetadata.model_validate(
            {
                "provider_id": "tushare",
                "provider_artifact_identity": (
                    "financial-provider-artifact:v1:" + "0" * 64
                ),
                "normalized_field": "gross_margin",
                "unit_normalization_contract_version": (
                    "fixture-indicator-normalizer-v1"
                ),
                "raw_value_currency_marker": "XXX",
                "raw_filing_consolidation_scope_marker": "UNKNOWN",
                "instrument_symbol": "601328.SS",
                "company_type": "bank",
                "period_end": "2025-12-31",
                "observed_at": "2026-05-01T00:00:00Z",
            }
        )


@pytest.mark.parametrize(
    "unsafe",
    [
        "ProviderError: token=fixture-secret",
        "https://provider.invalid?symbol=601328",
        r"C:\private\payload.json",
        "raw_payload_body",
    ],
)
def test_assessment_projection_rejects_error_query_path_and_payload_text(
    unsafe: str,
) -> None:
    with pytest.raises(ValidationError):
        FinancialIndicatorProviderFieldMetadata(
            provider_id="tushare",
            provider_artifact_identity=(
                "financial-provider-artifact:v1:" + "0" * 64
            ),
            normalized_field="gross_margin",
            unit_normalization_contract_version="fixture-indicator-normalizer-v1",
            raw_value_currency_marker=unsafe,
            raw_filing_consolidation_scope_marker="UNKNOWN",
        )

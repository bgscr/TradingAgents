from __future__ import annotations

import json
from datetime import date, datetime, timezone
from types import SimpleNamespace

import pandas as pd

from tradingagents.dataflows.financial_capability_routing import (
    FinancialIndicatorProviderGapRequest,
)
from tradingagents.dataflows.financial_contracts import (
    FinancialCompanyType,
    FinancialConsolidationScope,
    FinancialRatioFamily,
    resolve_financial_company_type,
)
from tradingagents.dataflows.financial_indicator_metadata import (
    FinancialIndicatorMetadataDimension,
    FinancialIndicatorSemanticAssessmentState,
)
from tradingagents.dataflows.qualified_production_sources import (
    AKSHARE_INDICATOR_NORMALIZER_VERSION,
    AKSHARE_INDICATOR_NORMALIZER_VERSION_V1,
    acquire_akshare_indicator_payload,
    decode_akshare_indicator_response,
)
from tradingagents.evidence import (
    IdentityProvenance,
    InstrumentIdentityEvidence,
    InstrumentKind,
)


def _identity() -> InstrumentIdentityEvidence:
    return InstrumentIdentityEvidence(
        symbol="601328.SS",
        venue="XSHG",
        instrument_kind=InstrumentKind.EQUITY,
        currency="CNY",
        provenance=IdentityProvenance(
            provider="fixture-registry",
            source_ref="registry:mainland:v1",
            retrieved_at="2026-07-29T00:00:00Z",
            artifact_sha256="a" * 64,
        ),
    )


def _request() -> FinancialIndicatorProviderGapRequest:
    resolution = resolve_financial_company_type(
        provider_declared_type=FinancialCompanyType.BANK,
        provider_declaration_qualified=True,
        classifier_metadata={},
        present_fields=("return_on_equity",),
    )
    return FinancialIndicatorProviderGapRequest(
        provider_id="akshare",
        instrument_identity=_identity(),
        as_of_date=date(2026, 7, 30),
        company_type=FinancialCompanyType.BANK,
        company_type_resolution=resolution,
        consolidation_scope=FinancialConsolidationScope.CONSOLIDATED,
        currency="CNY",
        missing_periods_by_family=(
            (FinancialRatioFamily.PROFIT, (date(2026, 3, 31),)),
        ),
    )


def test_akshare_v2_never_backfills_request_currency_or_scope() -> None:
    request = _request()
    facade = SimpleNamespace(
        stock_financial_analysis_indicator=lambda **_kwargs: pd.DataFrame(
            [{"日期": "2026-03-31", "净资产收益率(%)": "12.5"}]
        )
    )
    payload = acquire_akshare_indicator_payload(
        facade=facade,
        request=request,
        now=lambda: datetime(2026, 7, 30, 12, tzinfo=timezone.utc),
    )
    response = decode_akshare_indicator_response(
        request,
        payload,
        SimpleNamespace(key=SimpleNamespace(model_dump=lambda **_kwargs: {"k": "v"})),
    )

    assert AKSHARE_INDICATOR_NORMALIZER_VERSION == "akshare-financial-indicators-v2"
    candidate = response.candidates[0]
    assert candidate.period_identity.currency == "XXX"
    assert candidate.period_identity.currency != request.currency
    assert candidate.period_identity.consolidation_scope is (
        FinancialConsolidationScope.UNKNOWN
    )
    assert candidate.period_identity.consolidation_scope is not request.consolidation_scope
    assessment = response.indicator_metadata_compatibility[0]
    dimensions = {
        item.dimension: item
        for item in assessment.field_assessments[0].dimensions
    }
    assert dimensions[
        FinancialIndicatorMetadataDimension.VALUE_CURRENCY
    ].state is FinancialIndicatorSemanticAssessmentState.NOT_APPLICABLE
    assert dimensions[
        FinancialIndicatorMetadataDimension.FILING_CONSOLIDATION_SCOPE
    ].raw_provider_marker == "UNKNOWN"
    assert dimensions[
        FinancialIndicatorMetadataDimension.FILING_CONSOLIDATION_SCOPE
    ].state is FinancialIndicatorSemanticAssessmentState.UNPROVEN


def test_akshare_v1_payload_remains_readable_without_relabeling() -> None:
    request = _request()
    payload = acquire_akshare_indicator_payload(
        facade=SimpleNamespace(
            stock_financial_analysis_indicator=lambda **_kwargs: pd.DataFrame(
                [{"日期": "2026-03-31", "净资产收益率(%)": "12.5"}]
            )
        ),
        request=request,
        now=lambda: datetime(2026, 7, 30, 12, tzinfo=timezone.utc),
    )
    historical = json.loads(payload)
    historical["schema_identity"] = AKSHARE_INDICATOR_NORMALIZER_VERSION_V1

    response = decode_akshare_indicator_response(
        request,
        json.dumps(historical, sort_keys=True).encode("utf-8"),
        SimpleNamespace(key=SimpleNamespace(model_dump=lambda **_kwargs: {"k": "v"})),
    )

    assert response.response_artifact is not None
    assert (
        response.response_artifact.dataset.schema_identity
        == "akshare-financial-indicators-v1"
    )
    candidate = response.candidates[0]
    assert candidate.period_identity.currency == request.currency
    assert candidate.period_identity.consolidation_scope is request.consolidation_scope
    assert response.indicator_metadata_compatibility == ()

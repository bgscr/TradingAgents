from __future__ import annotations

from datetime import date, datetime, time, timedelta, timezone

import pytest

from tradingagents.dataflows._akshare_sina_statement_normalization import (
    AkshareSinaStatementTemporalProofSource,
)
from tradingagents.dataflows.financial_contracts import (
    FinancialFilingMetadata,
    FinancialPeriodCandidate,
    FinancialPeriodIdentity,
    FinancialProviderArtifactIdentity,
)
from tradingagents.dataflows.financial_temporal_eligibility import (
    EvidenceEffectiveAsOfDerivation,
    EvidenceEffectiveAsOfSemantics,
    FinancialMetadataTemporalEvidence,
    ProviderPublicationDateDerivation,
    ProviderPublicationDateSemantics,
    PublicationDateKind,
    TemporalEligibilityMode,
    TemporalEligibilityRejectionReason,
    TemporalEvidenceArtifactBinding,
    TemporalEvidenceClass,
    build_market_session_eligibility_binding,
)
from tradingagents.dataflows.financial_temporal_proof_projection import (
    FinancialEligibilityTemporalEvaluationTransaction,
    TemporalProofProjectionUnavailable,
    project_akshare_sina_company_type_temporal_evidence,
    project_baostock_listing_temporal_evidence,
    project_tushare_company_type_temporal_evidence,
    project_yahoo_current_profile_temporal_evidence,
)
from tradingagents.market_history.calendar import (
    MarketSession,
    MarketSessionStatus,
    ProvenanceClass,
    StoredSessionCalendar,
)

_SESSION = date(2026, 8, 10)
_RETRIEVED = datetime(2026, 8, 10, 16, 48, 7, tzinfo=timezone.utc)
_ARTIFACT_ID = "financial-provider-artifact:v1:" + "a" * 64


def _calendar() -> StoredSessionCalendar:
    return StoredSessionCalendar(
        calendar_revision_id="market-session-calendar:v1:" + "c" * 64,
        reference_market="mainland-cn",
        timezone_name="Asia/Shanghai",
        provider_dataset_id="provider-dataset:fixture-calendar-v1",
        observed_at=datetime(2026, 8, 1, tzinfo=timezone.utc),
        provenance_class=ProvenanceClass.OBSERVED_POINT_IN_TIME,
        sessions=(
            MarketSession(_SESSION, MarketSessionStatus.OPEN),
            MarketSession(date(2026, 8, 11), MarketSessionStatus.OPEN),
        ),
    )


def _candidate(
    *,
    ann_date: date | None = _SESSION,
    f_ann_date: date | None = _SESSION,
    period_end: date = date(2026, 6, 30),
    revision_identity: str = _ARTIFACT_ID,
) -> FinancialPeriodCandidate:
    artifact = FinancialProviderArtifactIdentity.model_construct(
        artifact_identity=_ARTIFACT_ID,
        observed_at=_RETRIEVED,
        retrieved_at=_RETRIEVED,
    )
    period = FinancialPeriodIdentity.model_construct(
        period_end=period_end,
        provider_revision_binding=revision_identity,
    )
    filing = FinancialFilingMetadata.model_construct(
        ann_date=ann_date,
        f_ann_date=f_ann_date,
        observed_at=_RETRIEVED,
        retrieved_at=_RETRIEVED,
        local_provider_revision_identity=revision_identity,
    )
    return FinancialPeriodCandidate.model_construct(
        artifact=artifact,
        period_identity=period,
        filing_metadata=filing,
        candidate_identity="financial-period-candidate:v1:" + "b" * 64,
    )


@pytest.mark.unit
def test_tushare_projects_exact_period_and_publication_fields_on_one_revision() -> None:
    evidence = project_tushare_company_type_temporal_evidence(_candidate())

    assert evidence.evidence_effective_as_of == date(2026, 6, 30)
    assert evidence.evidence_effective_as_of_derivation is not None
    assert evidence.evidence_effective_as_of_derivation.semantics is (
        EvidenceEffectiveAsOfSemantics.TUSHARE_STATEMENT_PERIOD_END
    )
    assert evidence.evidence_effective_as_of_derivation.source_field == "end_date"
    assert evidence.ann_date == _SESSION
    assert evidence.ann_date_derivation is not None
    assert evidence.ann_date_derivation.source_field == "ann_date"
    assert evidence.f_ann_date == _SESSION
    assert evidence.f_ann_date_derivation is not None
    assert evidence.f_ann_date_derivation.source_field == "f_ann_date"
    assert evidence.artifact.artifact_identity == _ARTIFACT_ID
    assert evidence.artifact.revision_identity == _ARTIFACT_ID
    assert evidence.artifact.row_identity == (
        "financial-period-candidate:v1:" + "b" * 64
    )
    assert evidence.observed_at == _RETRIEVED
    assert evidence.retrieved_at == _RETRIEVED


@pytest.mark.unit
@pytest.mark.parametrize(
    "candidate",
    (
        _candidate(ann_date=None),
        _candidate(revision_identity="financial-provider-artifact:v1:" + "d" * 64),
    ),
)
def test_tushare_missing_provider_announcement_or_revision_binding_fails_typed(
    candidate: FinancialPeriodCandidate,
) -> None:
    with pytest.raises(TemporalProofProjectionUnavailable):
        project_tushare_company_type_temporal_evidence(candidate)


@pytest.mark.unit
def test_akshare_projects_the_exact_provider_date_aliases() -> None:
    candidate = _candidate()
    source = AkshareSinaStatementTemporalProofSource(
        candidate_identity=candidate.candidate_identity,
        artifact_identity=candidate.artifact.artifact_identity,
        revision_identity=candidate.artifact.artifact_identity,
        row_identity=candidate.candidate_identity,
        evidence_effective_as_of=candidate.period_identity.period_end,
        evidence_effective_as_of_source_field="报告日期",
        ann_date=candidate.filing_metadata.ann_date,
        ann_date_source_field="公告日期",
        f_ann_date=candidate.filing_metadata.f_ann_date,
        f_ann_date_source_field="首次披露日期",
    )

    evidence = project_akshare_sina_company_type_temporal_evidence(
        candidate,
        source,
    )

    assert evidence.evidence_effective_as_of_derivation is not None
    assert evidence.evidence_effective_as_of_derivation.source_field == "report_period"
    assert evidence.ann_date_derivation is not None
    assert evidence.ann_date_derivation.source_field == "公告日期"
    assert evidence.f_ann_date_derivation is not None
    assert evidence.f_ann_date_derivation.source_field == "首次披露日期"


@pytest.mark.unit
def test_akshare_missing_explicit_announcement_proof_fails_without_backfill() -> None:
    candidate = _candidate(ann_date=None)
    source = AkshareSinaStatementTemporalProofSource(
        candidate_identity=candidate.candidate_identity,
        artifact_identity=candidate.artifact.artifact_identity,
        revision_identity=candidate.artifact.artifact_identity,
        row_identity=candidate.candidate_identity,
        evidence_effective_as_of=candidate.period_identity.period_end,
        evidence_effective_as_of_source_field="报告日",
    )

    with pytest.raises(TemporalProofProjectionUnavailable):
        project_akshare_sina_company_type_temporal_evidence(candidate, source)

    assert candidate.filing_metadata.observed_at == _RETRIEVED
    assert candidate.filing_metadata.retrieved_at == _RETRIEVED
    assert candidate.period_identity.period_end != _SESSION


@pytest.mark.unit
def test_yahoo_current_profile_is_diagnostic_only_and_never_temporally_qualified() -> None:
    evidence = project_yahoo_current_profile_temporal_evidence(
        artifact_identity="financial-provider-artifact:v1:" + "e" * 64,
        row_identity="yahoo-current-profile-row",
        observed_at=_RETRIEVED,
        retrieved_at=_RETRIEVED,
    )

    assert evidence.evidence_class is (
        TemporalEvidenceClass.YFINANCE_CURRENT_PROFILE_COMPANY_TYPE
    )
    assert evidence.evidence_effective_as_of is None
    assert evidence.ann_date is None
    assert evidence.f_ann_date is None
    assert evidence.observed_at == _RETRIEVED
    assert evidence.retrieved_at == _RETRIEVED


@pytest.mark.unit
def test_baostock_ipodate_is_effective_proof_but_cannot_synthesize_ann_date() -> None:
    evidence = project_baostock_listing_temporal_evidence(
        artifact_identity="provider-artifact:v1:" + "f" * 64,
        row_identity="provider-row-artifact:v1:" + "1" * 64,
        listing_date=date(2010, 1, 4),
        observed_at=_RETRIEVED,
        retrieved_at=_RETRIEVED,
    )

    assert evidence.evidence_effective_as_of == date(2010, 1, 4)
    assert evidence.evidence_effective_as_of_derivation is not None
    assert evidence.evidence_effective_as_of_derivation.semantics is (
        EvidenceEffectiveAsOfSemantics.BAOSTOCK_LIFECYCLE_LISTING_DATE
    )
    assert evidence.evidence_effective_as_of_derivation.source_field == "ipoDate"
    assert evidence.ann_date is None
    assert evidence.ann_date_derivation is None


def _qualified_company_evidence() -> FinancialMetadataTemporalEvidence:
    artifact = TemporalEvidenceArtifactBinding(
        artifact_identity=_ARTIFACT_ID,
        revision_identity=_ARTIFACT_ID,
        row_identity="financial-period-candidate:v1:" + "b" * 64,
    )
    return FinancialMetadataTemporalEvidence(
        evidence_class=TemporalEvidenceClass.TUSHARE_STATEMENT_COMPANY_TYPE,
        artifact=artifact,
        evidence_effective_as_of=date(2026, 6, 30),
        evidence_effective_as_of_derivation=EvidenceEffectiveAsOfDerivation(
            semantics=EvidenceEffectiveAsOfSemantics.TUSHARE_STATEMENT_PERIOD_END,
            source_field="end_date",
            artifact=artifact,
        ),
        ann_date=_SESSION,
        ann_date_derivation=ProviderPublicationDateDerivation(
            date_kind=PublicationDateKind.ANN_DATE,
            semantics=(
                ProviderPublicationDateSemantics.TUSHARE_STATEMENT_PUBLICATION_FIELDS
            ),
            source_field="ann_date",
            artifact=artifact,
        ),
        f_ann_date=_SESSION,
        f_ann_date_derivation=ProviderPublicationDateDerivation(
            date_kind=PublicationDateKind.F_ANN_DATE,
            semantics=(
                ProviderPublicationDateSemantics.TUSHARE_STATEMENT_PUBLICATION_FIELDS
            ),
            source_field="f_ann_date",
            artifact=artifact,
        ),
        observed_at=_RETRIEVED,
        retrieved_at=_RETRIEVED,
    )


@pytest.mark.unit
def test_real_next_day_0048_uses_frontier_not_retrieval_local_date() -> None:
    evaluated_at = datetime(2026, 8, 11, 1, tzinfo=timezone.utc)
    binding = build_market_session_eligibility_binding(
        _calendar(),
        requested_session=_SESSION,
        eligibility_evaluated_at=evaluated_at,
    )
    transaction = FinancialEligibilityTemporalEvaluationTransaction(
        mode=TemporalEligibilityMode.CURRENT_ANALYSIS_ONLY,
        calendar_binding=binding,
    )

    decision = transaction.evaluate(_qualified_company_evidence())

    assert decision.result == "accepted"
    assert decision.eligible_session == _SESSION
    assert decision.retrieved_at.astimezone().tzinfo is not None
    assert transaction.evaluation_count == 1


@pytest.mark.unit
def test_same_0048_evidence_rejects_strict_after_session_cutoff() -> None:
    binding = build_market_session_eligibility_binding(
        _calendar(),
        requested_session=_SESSION,
        eligibility_evaluated_at=datetime(2026, 8, 11, 1, tzinfo=timezone.utc),
    )
    strict_cutoff = datetime.combine(
        _SESSION,
        time(15),
        tzinfo=timezone.utc,
    )
    transaction = FinancialEligibilityTemporalEvaluationTransaction(
        mode=TemporalEligibilityMode.STRICT_HISTORICAL_PIT,
        calendar_binding=binding,
        strict_cutoff=strict_cutoff,
    )

    decision = transaction.evaluate(_qualified_company_evidence())

    assert decision.result == "rejected"
    assert (
        TemporalEligibilityRejectionReason.EXACT_REVISION_NOT_AVAILABLE_BY_STRICT_CUTOFF
        in decision.reasons
    )


@pytest.mark.unit
def test_windowed_transaction_rejects_proof_staged_by_another_run() -> None:
    common = {
        "mode": TemporalEligibilityMode.CURRENT_ANALYSIS_ONLY,
        "calendar": _calendar(),
        "requested_session": _SESSION,
        "eligibility_started_at": _RETRIEVED,
        "clock": lambda: _RETRIEVED + timedelta(seconds=1),
        "routing_plan_signature": "mainland-routing-plan:v1:" + "d" * 64,
    }
    first = FinancialEligibilityTemporalEvaluationTransaction(
        **common,
        canonical_run_id="run:" + "1" * 64,
    )
    second = FinancialEligibilityTemporalEvaluationTransaction(
        **common,
        canonical_run_id="run:" + "2" * 64,
    )
    staged = first.stage(_qualified_company_evidence(), evidence_origin="fresh")

    with pytest.raises(ValueError, match="active temporal transaction"):
        second.finalize((staged,))

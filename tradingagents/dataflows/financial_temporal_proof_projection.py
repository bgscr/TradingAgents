"""Exact provider-proof projection for financial eligibility metadata.

This module owns no clock, calendar lookup, provider route, or fallback.  It
only projects immutable provider-origin material and delegates every temporal
decision to the Ticket 02G evaluator.
"""

from __future__ import annotations

from datetime import date, datetime
from enum import Enum
from zoneinfo import ZoneInfo

from tradingagents.dataflows._akshare_sina_statement_normalization import (
    AkshareSinaStatementTemporalProofSource,
)
from tradingagents.dataflows.financial_contracts import FinancialPeriodCandidate
from tradingagents.dataflows.financial_temporal_eligibility import (
    EvidenceEffectiveAsOfDerivation,
    EvidenceEffectiveAsOfSemantics,
    ExactRevisionAvailabilityProof,
    FinancialMetadataTemporalEligibilityDecision,
    FinancialMetadataTemporalEvidence,
    MarketSessionEligibilityBinding,
    ProviderPublicationDateDerivation,
    ProviderPublicationDateSemantics,
    PublicationDateKind,
    TemporalEligibilityMode,
    TemporalEvidenceArtifactBinding,
    TemporalEvidenceClass,
    evaluate_financial_metadata_temporal_eligibility,
)


class TemporalProofProjectionUnavailableReason(str, Enum):
    """Payload-free structural reasons before temporal evaluation."""

    INVALID_CANDIDATE_BINDING = "invalid_candidate_binding"
    EFFECTIVE_PROOF_UNAVAILABLE = "effective_proof_unavailable"
    ANNOUNCEMENT_PROOF_UNAVAILABLE = "announcement_proof_unavailable"
    ARTIFACT_BINDING_UNAVAILABLE = "artifact_binding_unavailable"


class TemporalProofProjectionUnavailable(ValueError):
    """Typed structural projection failure without provider payload text."""

    def __init__(self, reason: TemporalProofProjectionUnavailableReason) -> None:
        self.reason = reason
        self.diagnostic_code = reason.value
        super().__init__(reason.value)


def _statement_artifact_binding(
    candidate: FinancialPeriodCandidate,
) -> TemporalEvidenceArtifactBinding:
    if not isinstance(candidate, FinancialPeriodCandidate):
        raise TemporalProofProjectionUnavailable(
            TemporalProofProjectionUnavailableReason.INVALID_CANDIDATE_BINDING
        )
    try:
        artifact_identity = candidate.artifact.artifact_identity
        period_revision = candidate.period_identity.provider_revision_binding
        filing_revision = candidate.filing_metadata.local_provider_revision_identity
        row_identity = candidate.candidate_identity
    except (AttributeError, TypeError, ValueError):
        raise TemporalProofProjectionUnavailable(
            TemporalProofProjectionUnavailableReason.ARTIFACT_BINDING_UNAVAILABLE
        ) from None
    if (
        not artifact_identity
        or period_revision != artifact_identity
        or filing_revision != artifact_identity
        or not row_identity
    ):
        raise TemporalProofProjectionUnavailable(
            TemporalProofProjectionUnavailableReason.ARTIFACT_BINDING_UNAVAILABLE
        )
    try:
        return TemporalEvidenceArtifactBinding(
            artifact_identity=artifact_identity,
            revision_identity=filing_revision,
            row_identity=row_identity,
        )
    except (TypeError, ValueError):
        raise TemporalProofProjectionUnavailable(
            TemporalProofProjectionUnavailableReason.ARTIFACT_BINDING_UNAVAILABLE
        ) from None


def _statement_temporal_evidence(
    candidate: FinancialPeriodCandidate,
    *,
    evidence_class: TemporalEvidenceClass,
    effective_semantics: EvidenceEffectiveAsOfSemantics,
    effective_source_field: str,
    publication_semantics: ProviderPublicationDateSemantics,
    ann_date_source_field: str = "ann_date",
    f_ann_date_source_field: str = "f_ann_date",
) -> FinancialMetadataTemporalEvidence:
    artifact = _statement_artifact_binding(candidate)
    try:
        period_end = candidate.period_identity.period_end
        filing = candidate.filing_metadata
        ann_date = filing.ann_date
        f_ann_date = filing.f_ann_date
    except (AttributeError, TypeError, ValueError):
        raise TemporalProofProjectionUnavailable(
            TemporalProofProjectionUnavailableReason.INVALID_CANDIDATE_BINDING
        ) from None
    if not isinstance(period_end, date):
        raise TemporalProofProjectionUnavailable(
            TemporalProofProjectionUnavailableReason.EFFECTIVE_PROOF_UNAVAILABLE
        )
    if not isinstance(ann_date, date):
        raise TemporalProofProjectionUnavailable(
            TemporalProofProjectionUnavailableReason.ANNOUNCEMENT_PROOF_UNAVAILABLE
        )
    ann_derivation = ProviderPublicationDateDerivation(
        date_kind=PublicationDateKind.ANN_DATE,
        semantics=publication_semantics,
        source_field=ann_date_source_field,
        artifact=artifact,
    )
    f_ann_derivation = (
        ProviderPublicationDateDerivation(
            date_kind=PublicationDateKind.F_ANN_DATE,
            semantics=publication_semantics,
            source_field=f_ann_date_source_field,
            artifact=artifact,
        )
        if isinstance(f_ann_date, date)
        else None
    )
    return FinancialMetadataTemporalEvidence(
        evidence_class=evidence_class,
        artifact=artifact,
        evidence_effective_as_of=period_end,
        evidence_effective_as_of_derivation=EvidenceEffectiveAsOfDerivation(
            semantics=effective_semantics,
            source_field=effective_source_field,
            artifact=artifact,
        ),
        ann_date=ann_date,
        ann_date_derivation=ann_derivation,
        f_ann_date=f_ann_date if isinstance(f_ann_date, date) else None,
        f_ann_date_derivation=f_ann_derivation,
        observed_at=filing.observed_at,
        retrieved_at=filing.retrieved_at,
        retrieval_local_date=filing.retrieved_at.astimezone(
            ZoneInfo("Asia/Shanghai")
        ).date(),
    )


def project_tushare_company_type_temporal_evidence(
    candidate: FinancialPeriodCandidate,
) -> FinancialMetadataTemporalEvidence:
    """Project exact Tushare statement-period and publication proof."""

    return _statement_temporal_evidence(
        candidate,
        evidence_class=TemporalEvidenceClass.TUSHARE_STATEMENT_COMPANY_TYPE,
        effective_semantics=EvidenceEffectiveAsOfSemantics.TUSHARE_STATEMENT_PERIOD_END,
        effective_source_field="end_date",
        publication_semantics=(
            ProviderPublicationDateSemantics.TUSHARE_STATEMENT_PUBLICATION_FIELDS
        ),
    )


def project_akshare_sina_company_type_temporal_evidence(
    candidate: FinancialPeriodCandidate,
    source: AkshareSinaStatementTemporalProofSource,
) -> FinancialMetadataTemporalEvidence:
    """Project only normalized explicit AKShare-Sina statement proof."""

    if (
        source.candidate_identity != candidate.candidate_identity
        or source.artifact_identity != candidate.artifact.artifact_identity
        or source.revision_identity
        != candidate.filing_metadata.local_provider_revision_identity
        or source.row_identity != candidate.candidate_identity
        or source.evidence_effective_as_of
        != candidate.period_identity.period_end
        or source.ann_date != candidate.filing_metadata.ann_date
        or source.f_ann_date != candidate.filing_metadata.f_ann_date
    ):
        raise TemporalProofProjectionUnavailable(
            TemporalProofProjectionUnavailableReason.ARTIFACT_BINDING_UNAVAILABLE
        )

    return _statement_temporal_evidence(
        candidate,
        evidence_class=TemporalEvidenceClass.AKSHARE_SINA_STATEMENT_COMPANY_TYPE,
        effective_semantics=(
            EvidenceEffectiveAsOfSemantics.AKSHARE_SINA_STATEMENT_PERIOD_END
        ),
        effective_source_field="report_period",
        publication_semantics=(
            ProviderPublicationDateSemantics.AKSHARE_SINA_STATEMENT_PUBLICATION_FIELDS
        ),
        ann_date_source_field=source.ann_date_source_field or "ann_date",
        f_ann_date_source_field=source.f_ann_date_source_field or "f_ann_date",
    )


def project_yahoo_current_profile_temporal_evidence(
    *,
    artifact_identity: str,
    row_identity: str,
    observed_at: datetime,
    retrieved_at: datetime,
) -> FinancialMetadataTemporalEvidence:
    """Retain Yahoo timestamps diagnostically without inventing proof."""

    artifact = TemporalEvidenceArtifactBinding(
        artifact_identity=artifact_identity,
        revision_identity=artifact_identity,
        row_identity=row_identity,
    )
    return FinancialMetadataTemporalEvidence(
        evidence_class=TemporalEvidenceClass.YFINANCE_CURRENT_PROFILE_COMPANY_TYPE,
        artifact=artifact,
        observed_at=observed_at,
        retrieved_at=retrieved_at,
        retrieval_local_date=retrieved_at.astimezone(
            ZoneInfo("Asia/Shanghai")
        ).date(),
    )


def project_baostock_listing_temporal_evidence(
    *,
    artifact_identity: str,
    row_identity: str,
    listing_date: date,
    observed_at: datetime,
    retrieved_at: datetime,
    ann_date: date | None = None,
    ann_date_source_field: str | None = None,
    f_ann_date: date | None = None,
    f_ann_date_source_field: str | None = None,
    exact_revision_availability_proof: ExactRevisionAvailabilityProof | None = None,
) -> FinancialMetadataTemporalEvidence:
    """Project BaoStock ipoDate without aliasing it to publication proof."""

    artifact = TemporalEvidenceArtifactBinding(
        artifact_identity=artifact_identity,
        revision_identity=artifact_identity,
        row_identity=row_identity,
    )
    ann_derivation = (
        ProviderPublicationDateDerivation(
            date_kind=PublicationDateKind.ANN_DATE,
            semantics=(
                ProviderPublicationDateSemantics.BAOSTOCK_LIFECYCLE_PUBLICATION_FIELDS
            ),
            source_field=ann_date_source_field,
            artifact=artifact,
        )
        if ann_date is not None and ann_date_source_field is not None
        else None
    )
    first_derivation = (
        ProviderPublicationDateDerivation(
            date_kind=PublicationDateKind.F_ANN_DATE,
            semantics=(
                ProviderPublicationDateSemantics.BAOSTOCK_LIFECYCLE_PUBLICATION_FIELDS
            ),
            source_field=f_ann_date_source_field,
            artifact=artifact,
        )
        if f_ann_date is not None and f_ann_date_source_field is not None
        else None
    )
    if (ann_date is None) != (ann_derivation is None) or (
        f_ann_date is None
    ) != (first_derivation is None):
        raise TemporalProofProjectionUnavailable(
            TemporalProofProjectionUnavailableReason.ANNOUNCEMENT_PROOF_UNAVAILABLE
        )
    return FinancialMetadataTemporalEvidence(
        evidence_class=TemporalEvidenceClass.BAOSTOCK_ISSUER_LIFECYCLE_LISTING,
        artifact=artifact,
        evidence_effective_as_of=listing_date,
        evidence_effective_as_of_derivation=EvidenceEffectiveAsOfDerivation(
            semantics=EvidenceEffectiveAsOfSemantics.BAOSTOCK_LIFECYCLE_LISTING_DATE,
            source_field="ipoDate",
            artifact=artifact,
        ),
        ann_date=ann_date,
        ann_date_derivation=ann_derivation,
        f_ann_date=f_ann_date,
        f_ann_date_derivation=first_derivation,
        observed_at=observed_at,
        retrieved_at=retrieved_at,
        exact_revision_availability_proof=exact_revision_availability_proof,
        retrieval_local_date=retrieved_at.astimezone(
            ZoneInfo("Asia/Shanghai")
        ).date(),
    )


class FinancialEligibilityTemporalEvaluationTransaction:
    """One run-owned transaction delegating every decision to Ticket 02G."""

    def __init__(
        self,
        *,
        mode: TemporalEligibilityMode,
        calendar_binding: MarketSessionEligibilityBinding,
        strict_cutoff: datetime | None = None,
    ) -> None:
        self._mode = TemporalEligibilityMode(mode)
        self._calendar_binding = MarketSessionEligibilityBinding.model_validate(
            calendar_binding.model_dump(mode="json")
        )
        self._strict_cutoff = strict_cutoff
        self._evaluation_count = 0

    @property
    def evaluation_count(self) -> int:
        return self._evaluation_count

    @property
    def mode(self) -> TemporalEligibilityMode:
        return self._mode

    @property
    def calendar_binding(self) -> MarketSessionEligibilityBinding:
        return self._calendar_binding

    @property
    def strict_cutoff(self) -> datetime | None:
        return self._strict_cutoff

    def evaluate(
        self,
        evidence: FinancialMetadataTemporalEvidence,
    ) -> FinancialMetadataTemporalEligibilityDecision:
        decision = evaluate_financial_metadata_temporal_eligibility(
            mode=self._mode,
            evidence=evidence,
            calendar_binding=self._calendar_binding,
            strict_cutoff=self._strict_cutoff,
        )
        self._evaluation_count += 1
        return decision


__all__ = [
    "FinancialEligibilityTemporalEvaluationTransaction",
    "TemporalProofProjectionUnavailable",
    "TemporalProofProjectionUnavailableReason",
    "project_akshare_sina_company_type_temporal_evidence",
    "project_baostock_listing_temporal_evidence",
    "project_tushare_company_type_temporal_evidence",
    "project_yahoo_current_profile_temporal_evidence",
]

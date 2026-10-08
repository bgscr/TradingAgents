"""Exact provider-proof projection for financial eligibility metadata.

This module owns no provider route or fallback. It projects immutable
provider-origin material and, for plan 1.4, seals one injected-clock acquisition
window over one already-pinned in-memory calendar.
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
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
    FinancialEligibilityAcquisitionWindow,
    FinancialEligibilityTemporalTransactionBinding,
    FinancialMetadataTemporalEligibilityDecision,
    FinancialMetadataTemporalEvidence,
    FinancialTemporalEvidenceAcquisitionBinding,
    MarketSessionEligibilityBinding,
    ProviderPublicationDateDerivation,
    ProviderPublicationDateSemantics,
    PublicationDateKind,
    TemporalEligibilityMode,
    TemporalEvidenceAcquisitionOrigin,
    TemporalEvidenceArtifactBinding,
    TemporalEvidenceClass,
    bind_financial_temporal_evidence_to_transaction,
    build_financial_eligibility_acquisition_window,
    build_financial_eligibility_temporal_transaction_binding,
    build_market_session_eligibility_binding,
    evaluate_financial_metadata_temporal_eligibility,
    evaluate_financial_metadata_temporal_eligibility_v2,
    qualify_financial_metadata_temporal_evidence_for_acquisition,
)
from tradingagents.dataflows.listing_publication_evidence import (
    ListingEffectiveEvidence,
    ListingEvidenceArtifactBinding,
)
from tradingagents.evidence import InstrumentIdentityEvidence
from tradingagents.market_history import StoredSessionCalendar


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


def project_baostock_listing_effective_evidence(
    *,
    instrument_identity: InstrumentIdentityEvidence,
    artifact_identity: str,
    artifact_sha256: str,
    artifact_byte_length: int,
    artifact_media_type: str,
    row_identity: str,
    listing_date: date,
    lifecycle_status: str,
    observed_at: datetime,
    retrieved_at: datetime,
) -> ListingEffectiveEvidence:
    """Project only BaoStock's lifecycle-effective fact for composite proof v2."""

    return ListingEffectiveEvidence(
        instrument_identity=instrument_identity,
        provider_id="baostock",
        venue=instrument_identity.venue,
        listing_effective_date=listing_date,
        lifecycle_status=lifecycle_status,
        source_field="ipoDate",
        artifact=ListingEvidenceArtifactBinding(
            artifact_identity=artifact_identity,
            artifact_sha256=artifact_sha256,
            revision_identity=artifact_identity,
            row_identity=row_identity,
            byte_length=artifact_byte_length,
            media_type=artifact_media_type,
        ),
        observed_at=observed_at,
        retrieved_at=retrieved_at,
    )


class FinancialEligibilityTemporalEvaluationTransaction:
    """One run-owned transaction delegating every decision to Ticket 02G."""

    def __init__(
        self,
        *,
        mode: TemporalEligibilityMode,
        calendar_binding: MarketSessionEligibilityBinding | None = None,
        strict_cutoff: datetime | None = None,
        calendar: StoredSessionCalendar | None = None,
        requested_session: date | None = None,
        eligibility_started_at: datetime | None = None,
        clock: Callable[[], datetime] | None = None,
        canonical_run_id: str | None = None,
        routing_plan_signature: str | None = None,
    ) -> None:
        self._mode = TemporalEligibilityMode(mode)
        self._calendar = calendar
        self._requested_session = requested_session
        self._eligibility_started_at = eligibility_started_at
        self._clock = clock
        self._window: FinancialEligibilityAcquisitionWindow | None = None
        self._transaction_binding: (
            FinancialEligibilityTemporalTransactionBinding | None
        ) = None
        self._finalized = False
        v2 = any(
            value is not None
            for value in (calendar, requested_session, eligibility_started_at, clock)
        )
        if v2:
            if (
                calendar_binding is not None
                or not isinstance(calendar, StoredSessionCalendar)
                or not isinstance(requested_session, date)
                or isinstance(requested_session, datetime)
                or not isinstance(eligibility_started_at, datetime)
                or not callable(clock)
                or canonical_run_id is None
                or routing_plan_signature is None
            ):
                raise ValueError("temporal v2 transaction binding is incomplete")
            self._calendar_binding = build_market_session_eligibility_binding(
                calendar,
                requested_session=requested_session,
                eligibility_evaluated_at=eligibility_started_at,
            )
            self._transaction_binding = (
                build_financial_eligibility_temporal_transaction_binding(
                    canonical_run_id=canonical_run_id,
                    routing_plan_signature=routing_plan_signature,
                    mode=self._mode,
                    requested_session=requested_session,
                    eligibility_started_at=eligibility_started_at,
                    calendar_binding=self._calendar_binding,
                    strict_cutoff=strict_cutoff,
                )
            )
        else:
            if (
                calendar_binding is None
                or canonical_run_id is not None
                or routing_plan_signature is not None
            ):
                raise ValueError("temporal v1 transaction requires a calendar binding")
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

    @property
    def acquisition_window(self) -> FinancialEligibilityAcquisitionWindow | None:
        return self._window

    @property
    def transaction_binding(
        self,
    ) -> FinancialEligibilityTemporalTransactionBinding | None:
        return self._transaction_binding

    @property
    def transaction_identity(self) -> str | None:
        binding = self._transaction_binding
        return binding.transaction_identity if binding is not None else None

    @property
    def is_v2(self) -> bool:
        return self._calendar is not None

    def evaluate(
        self,
        evidence: FinancialMetadataTemporalEvidence,
    ) -> FinancialMetadataTemporalEligibilityDecision:
        if self._finalized:
            raise ValueError("temporal transaction is already finalized")
        if self._calendar is not None:
            raise ValueError("temporal v2 proof must be staged before finalization")
        decision = evaluate_financial_metadata_temporal_eligibility(
            mode=self._mode,
            evidence=evidence,
            calendar_binding=self._calendar_binding,
            strict_cutoff=self._strict_cutoff,
        )
        self._evaluation_count += 1
        return decision

    def qualifies(
        self,
        evidence: FinancialMetadataTemporalEvidence,
        *,
        evidence_origin: TemporalEvidenceAcquisitionOrigin | str = (
            TemporalEvidenceAcquisitionOrigin.FRESH
        ),
    ) -> bool:
        """Stage structurally eligible v2 proof without sampling a terminal clock."""

        if self._calendar is None or self._finalized:
            raise ValueError("temporal proof staging requires an open v2 transaction")
        return not qualify_financial_metadata_temporal_evidence_for_acquisition(
            mode=self._mode,
            evidence=evidence,
            started_calendar_binding=self._calendar_binding,
            evidence_origin=TemporalEvidenceAcquisitionOrigin(evidence_origin),
            strict_cutoff=self._strict_cutoff,
        )

    def stage(
        self,
        evidence: FinancialMetadataTemporalEvidence,
        *,
        evidence_origin: TemporalEvidenceAcquisitionOrigin | str,
    ) -> FinancialTemporalEvidenceAcquisitionBinding:
        """Bind one qualified proof to this still-open transaction."""

        parsed_origin = TemporalEvidenceAcquisitionOrigin(evidence_origin)
        if not self.qualifies(evidence, evidence_origin=parsed_origin):
            raise ValueError("temporal proof is not qualified for acquisition")
        binding = self._transaction_binding
        if binding is None:
            raise ValueError("temporal proof staging has no active transaction")
        return bind_financial_temporal_evidence_to_transaction(
            transaction_identity=binding.transaction_identity,
            evidence_origin=parsed_origin,
            evidence=evidence,
        )

    def finalize(
        self,
        evidence: Sequence[FinancialTemporalEvidenceAcquisitionBinding],
    ) -> tuple[FinancialMetadataTemporalEligibilityDecision, ...]:
        """Seal v2 once and emit authoritative decisions for selected proofs."""

        if self._calendar is None:
            raise ValueError("temporal v1 transaction has no acquisition window")
        if self._finalized or not evidence:
            raise ValueError("temporal transaction cannot be finalized")
        assert self._requested_session is not None
        assert self._eligibility_started_at is not None
        assert self._clock is not None
        transaction_binding = self._transaction_binding
        if transaction_binding is None or any(
            item.transaction_identity != transaction_binding.transaction_identity
            for item in evidence
        ):
            raise ValueError("proof is not bound to the active temporal transaction")
        window = build_financial_eligibility_acquisition_window(
            self._calendar,
            transaction_binding=transaction_binding,
            requested_session=self._requested_session,
            eligibility_started_at=self._eligibility_started_at,
            eligibility_evaluated_at=self._clock(),
        )
        decisions = tuple(
            evaluate_financial_metadata_temporal_eligibility_v2(
                mode=self._mode,
                evidence=item.evidence,
                acquisition_window=window,
                evidence_origin=item.evidence_origin,
                evidence_acquisition_binding=item,
                strict_cutoff=self._strict_cutoff,
            )
            for item in evidence
        )
        self._window = window
        self._finalized = True
        self._evaluation_count += len(decisions)
        return decisions


__all__ = [
    "FinancialEligibilityTemporalEvaluationTransaction",
    "TemporalProofProjectionUnavailable",
    "TemporalProofProjectionUnavailableReason",
    "project_akshare_sina_company_type_temporal_evidence",
    "project_baostock_listing_temporal_evidence",
    "project_baostock_listing_effective_evidence",
    "project_tushare_company_type_temporal_evidence",
    "project_yahoo_current_profile_temporal_evidence",
]

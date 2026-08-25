from __future__ import annotations

from datetime import date, datetime, timezone
from inspect import signature
from zoneinfo import ZoneInfo

import pytest
from pydantic import ValidationError

from tradingagents.dataflows import financial_temporal_eligibility as temporal_module
from tradingagents.dataflows.financial_temporal_eligibility import (
    FINANCIAL_METADATA_TEMPORAL_ELIGIBILITY_DECISION_VERSION,
    PROVIDER_NEUTRAL_TEMPORAL_ELIGIBILITY_VERSION,
    EvidenceEffectiveAsOfDerivation,
    ExactRevisionAvailabilityProof,
    ExactRevisionAvailabilityScope,
    FinancialMetadataTemporalEligibilityDecision,
    FinancialMetadataTemporalEvidence,
    ProviderPublicationDateDerivation,
    ProviderPublicationDateSemantics,
    PublicationDateKind,
    TemporalEligibilityMode,
    TemporalEligibilityRejectionReason,
    TemporalEligibilityRejectionReasonV2,
    TemporalEvidenceArtifactBinding,
    TemporalEvidenceClass,
    build_financial_eligibility_acquisition_window,
    build_financial_eligibility_temporal_transaction_binding,
    build_market_session_eligibility_binding,
    evaluate_financial_metadata_temporal_eligibility,
    evaluate_financial_metadata_temporal_eligibility_v2,
)
from tradingagents.market_history import (
    MarketSession,
    MarketSessionStatus,
    ProvenanceClass,
    StoredSessionCalendar,
)

SHANGHAI = ZoneInfo("Asia/Shanghai")


def _at(day: int, hour: int, minute: int = 0) -> datetime:
    return datetime(2026, 8, day, hour, minute, tzinfo=SHANGHAI)


_STRICT_CUTOFF = _at(10, 15)


def _transaction_binding(
    *,
    eligibility_started_at: datetime,
    mode: TemporalEligibilityMode = TemporalEligibilityMode.CURRENT_ANALYSIS_ONLY,
    strict_cutoff: datetime | None = None,
):
    calendar_binding = build_market_session_eligibility_binding(
        _calendar(),
        requested_session=date(2026, 8, 10),
        eligibility_evaluated_at=eligibility_started_at,
    )
    return build_financial_eligibility_temporal_transaction_binding(
        canonical_run_id="run:" + "1" * 64,
        routing_plan_signature="mainland-routing-plan:v1:" + "2" * 64,
        mode=mode,
        requested_session=date(2026, 8, 10),
        eligibility_started_at=eligibility_started_at,
        calendar_binding=calendar_binding,
        strict_cutoff=strict_cutoff,
    )


def _calendar() -> StoredSessionCalendar:
    return StoredSessionCalendar(
        calendar_revision_id="market-session-calendar:v1:" + "a" * 64,
        reference_market="mainland-cn",
        timezone_name="Asia/Shanghai",
        provider_dataset_id="provider-dataset:baostock-calendar-v1",
        observed_at=datetime(2026, 8, 1, tzinfo=timezone.utc),
        provenance_class=ProvenanceClass.OBSERVED_POINT_IN_TIME,
        sessions=(
            MarketSession(date(2026, 8, 7), MarketSessionStatus.OPEN),
            MarketSession(date(2026, 8, 8), MarketSessionStatus.CLOSED),
            MarketSession(date(2026, 8, 9), MarketSessionStatus.CLOSED),
            MarketSession(date(2026, 8, 10), MarketSessionStatus.OPEN),
            MarketSession(date(2026, 8, 11), MarketSessionStatus.OPEN),
            MarketSession(date(2026, 8, 12), MarketSessionStatus.CLOSED),
            MarketSession(date(2026, 8, 13), MarketSessionStatus.OPEN),
        ),
    )


def _artifact() -> TemporalEvidenceArtifactBinding:
    return TemporalEvidenceArtifactBinding(
        artifact_identity="financial-provider-artifact:v1:" + "b" * 64,
        revision_identity="financial-provider-revision:v1:" + "c" * 64,
        row_identity="row:company-type:2026-06-30",
    )


def _evidence(
    *,
    retrieved_at: datetime,
    observed_at: datetime | None = None,
    **changes: object,
) -> FinancialMetadataTemporalEvidence:
    artifact = _artifact()
    payload: dict[str, object] = {
        "evidence_class": TemporalEvidenceClass.TUSHARE_STATEMENT_COMPANY_TYPE,
        "artifact": artifact,
        "evidence_effective_as_of": date(2026, 6, 30),
        "evidence_effective_as_of_derivation": EvidenceEffectiveAsOfDerivation(
            semantics="tushare_statement_period_end",
            source_field="end_date",
            artifact=artifact,
        ),
        "ann_date": date(2026, 8, 7),
        "ann_date_derivation": ProviderPublicationDateDerivation(
            date_kind=PublicationDateKind.ANN_DATE,
            semantics=(ProviderPublicationDateSemantics.TUSHARE_STATEMENT_PUBLICATION_FIELDS),
            source_field="ann_date",
            artifact=artifact,
        ),
        "f_ann_date": date(2026, 8, 7),
        "f_ann_date_derivation": ProviderPublicationDateDerivation(
            date_kind=PublicationDateKind.F_ANN_DATE,
            semantics=(ProviderPublicationDateSemantics.TUSHARE_STATEMENT_PUBLICATION_FIELDS),
            source_field="f_ann_date",
            artifact=artifact,
        ),
        "observed_at": observed_at or retrieved_at,
        "retrieved_at": retrieved_at,
    }
    payload.update(changes)
    return FinancialMetadataTemporalEvidence(**payload)


def _current_decision(
    *,
    requested_session: date = date(2026, 8, 10),
    retrieved_at: datetime,
    evaluated_at: datetime,
    evidence: FinancialMetadataTemporalEvidence | None = None,
):
    binding = build_market_session_eligibility_binding(
        _calendar(),
        requested_session=requested_session,
        eligibility_evaluated_at=evaluated_at,
    )
    return evaluate_financial_metadata_temporal_eligibility(
        mode=TemporalEligibilityMode.CURRENT_ANALYSIS_ONLY,
        evidence=evidence or _evidence(retrieved_at=retrieved_at),
        calendar_binding=binding,
    )


@pytest.mark.unit
def test_valid_current_analysis_after_requested_session_close_is_accepted() -> None:
    evaluated_at = _at(10, 15, 5)
    binding = build_market_session_eligibility_binding(
        _calendar(),
        requested_session=date(2026, 8, 10),
        eligibility_evaluated_at=evaluated_at,
    )

    decision = evaluate_financial_metadata_temporal_eligibility(
        mode=TemporalEligibilityMode.CURRENT_ANALYSIS_ONLY,
        evidence=_evidence(retrieved_at=_at(10, 15)),
        calendar_binding=binding,
    )

    assert decision.contract_version == (FINANCIAL_METADATA_TEMPORAL_ELIGIBILITY_DECISION_VERSION)
    assert decision.temporal_policy_version == (PROVIDER_NEUTRAL_TEMPORAL_ELIGIBILITY_VERSION)
    assert decision.result == "accepted"
    assert decision.reasons == ()
    assert decision.eligible_session == date(2026, 8, 10)
    assert decision.decision_identity.startswith(
        "financial-metadata-temporal-eligibility-decision:v1:"
    )


@pytest.mark.unit
def test_windowed_current_analysis_rejects_terminal_clock_regression() -> None:
    with pytest.raises(
        ValidationError,
        match="market-session eligibility window binding is invalid",
    ):
        build_financial_eligibility_acquisition_window(
            _calendar(),
            transaction_binding=_transaction_binding(
                eligibility_started_at=_at(10, 15, 5)
            ),
            requested_session=date(2026, 8, 10),
            eligibility_started_at=_at(10, 15, 5),
            eligibility_evaluated_at=_at(10, 15, 4),
        )


@pytest.mark.unit
def test_windowed_current_analysis_rejects_truthful_future_retrieval() -> None:
    retrieved_at = _at(10, 15, 6)
    window = build_financial_eligibility_acquisition_window(
        _calendar(),
        transaction_binding=_transaction_binding(
            eligibility_started_at=_at(10, 15)
        ),
        requested_session=date(2026, 8, 10),
        eligibility_started_at=_at(10, 15),
        eligibility_evaluated_at=_at(10, 15, 5),
    )

    decision = evaluate_financial_metadata_temporal_eligibility_v2(
        mode=TemporalEligibilityMode.CURRENT_ANALYSIS_ONLY,
        evidence=_evidence(retrieved_at=retrieved_at),
        acquisition_window=window,
    )

    assert decision.result == "rejected"
    assert decision.retrieved_at == retrieved_at
    assert (
        TemporalEligibilityRejectionReason.RETRIEVAL_AFTER_ELIGIBILITY_EVALUATION
        in decision.reasons
    )


@pytest.mark.unit
def test_windowed_retained_proof_uses_terminal_bound_without_fresh_start_claim() -> None:
    window = build_financial_eligibility_acquisition_window(
        _calendar(),
        transaction_binding=_transaction_binding(
            eligibility_started_at=_at(10, 15, 5)
        ),
        requested_session=date(2026, 8, 10),
        eligibility_started_at=_at(10, 15, 5),
        eligibility_evaluated_at=_at(10, 15, 10),
    )
    evidence = _evidence(retrieved_at=_at(10, 15, 1))

    retained = evaluate_financial_metadata_temporal_eligibility_v2(
        mode=TemporalEligibilityMode.CURRENT_ANALYSIS_ONLY,
        evidence=evidence,
        acquisition_window=window,
        evidence_origin="retained",
    )
    fresh = evaluate_financial_metadata_temporal_eligibility_v2(
        mode=TemporalEligibilityMode.CURRENT_ANALYSIS_ONLY,
        evidence=evidence,
        acquisition_window=window,
        evidence_origin="fresh",
    )

    assert retained.result == "accepted"
    assert retained.reasons == ()
    assert retained.evidence_origin == "retained"
    assert fresh.result == "rejected"
    assert fresh.evidence_origin == "fresh"
    assert {
        TemporalEligibilityRejectionReasonV2.OBSERVATION_BEFORE_ELIGIBILITY_START,
        TemporalEligibilityRejectionReasonV2.RETRIEVAL_BEFORE_ELIGIBILITY_START,
    }.issubset(fresh.reasons)


@pytest.mark.unit
def test_windowed_strict_exact_revision_still_rejects_retrieval_after_terminal() -> None:
    retrieved_at = _at(10, 15, 11)
    artifact = _artifact()
    window = build_financial_eligibility_acquisition_window(
        _calendar(),
        transaction_binding=_transaction_binding(
            eligibility_started_at=_at(10, 15, 5),
            mode=TemporalEligibilityMode.STRICT_HISTORICAL_PIT,
            strict_cutoff=_STRICT_CUTOFF,
        ),
        requested_session=date(2026, 8, 10),
        eligibility_started_at=_at(10, 15, 5),
        eligibility_evaluated_at=_at(10, 15, 10),
    )
    evidence = _evidence(
        retrieved_at=retrieved_at,
        exact_revision_availability_proof=ExactRevisionAvailabilityProof(
            exact_revision_authoritative_available_at=_STRICT_CUTOFF,
            authoritative=True,
            scope=ExactRevisionAvailabilityScope.EXACT_IMMUTABLE_REVISION,
            proof_identity="strict-window-terminal-bound",
            artifact=artifact,
        ),
    )

    decision = evaluate_financial_metadata_temporal_eligibility_v2(
        mode=TemporalEligibilityMode.STRICT_HISTORICAL_PIT,
        evidence=evidence,
        acquisition_window=window,
        evidence_origin="retained",
        strict_cutoff=_STRICT_CUTOFF,
    )

    assert decision.result == "rejected"
    assert decision.retrieved_at == retrieved_at
    assert decision.evidence_origin == "retained"
    assert (
        TemporalEligibilityRejectionReason.RETRIEVAL_AFTER_ELIGIBILITY_EVALUATION
        in decision.reasons
    )


@pytest.mark.unit
def test_windowed_decision_rejects_mode_contradicting_transaction_binding() -> None:
    window = build_financial_eligibility_acquisition_window(
        _calendar(),
        transaction_binding=_transaction_binding(
            eligibility_started_at=_at(10, 15, 5)
        ),
        requested_session=date(2026, 8, 10),
        eligibility_started_at=_at(10, 15, 5),
        eligibility_evaluated_at=_at(10, 15, 10),
    )
    decision = evaluate_financial_metadata_temporal_eligibility_v2(
        mode=TemporalEligibilityMode.CURRENT_ANALYSIS_ONLY,
        evidence=_evidence(retrieved_at=_at(10, 15, 6)),
        acquisition_window=window,
    )
    payload = decision.model_dump(mode="python")
    payload["mode"] = TemporalEligibilityMode.STRICT_HISTORICAL_PIT.value

    with pytest.raises(
        ValidationError,
        match="temporal decision active transaction binding mismatch",
    ):
        FinancialMetadataTemporalEligibilityDecision.model_validate(payload)


@pytest.mark.unit
def test_canonical_store_calendar_revision_binds_without_relabeling() -> None:
    calendar = _calendar()
    canonical = StoredSessionCalendar(
        calendar_revision_id="market-session-calendar=sha256:" + "f" * 64,
        reference_market=calendar.reference_market,
        timezone_name=calendar.timezone_name,
        provider_dataset_id=calendar.provider_dataset_id,
        observed_at=calendar.observed_at,
        provenance_class=calendar.provenance_class,
        sessions=calendar.sessions,
    )

    binding = build_market_session_eligibility_binding(
        canonical,
        requested_session=date(2026, 8, 10),
        eligibility_evaluated_at=_at(10, 15, 5),
    )

    assert binding.calendar_revision_id == canonical.calendar_revision_id
    assert binding.has_valid_identity()


@pytest.mark.unit
@pytest.mark.parametrize(
    ("retrieved_at", "evaluated_at"),
    (
        (_at(11, 0, 48), _at(11, 0, 49)),
        (_at(11, 9, 29), _at(11, 9, 29)),
        (_at(11, 14, 59), _at(11, 14, 59)),
        (_at(7, 15), _at(8, 12)),
        (_at(7, 15), _at(9, 23, 59)),
        (_at(11, 15), _at(12, 12)),
        (_at(11, 15), _at(13, 14, 59)),
    ),
)
def test_current_analysis_uses_latest_completed_frontier_not_local_dates(
    retrieved_at: datetime,
    evaluated_at: datetime,
) -> None:
    requested = (
        date(2026, 8, 7)
        if retrieved_at.day == 7
        else date(2026, 8, 11)
        if retrieved_at.day == 11 and evaluated_at.day >= 12
        else date(2026, 8, 10)
    )
    decision = _current_decision(
        requested_session=requested,
        retrieved_at=retrieved_at,
        evaluated_at=evaluated_at,
    )

    assert decision.result == "accepted"


@pytest.mark.unit
@pytest.mark.parametrize("evaluated_at", (_at(11, 15), _at(11, 15, 1)))
def test_next_session_completion_advances_the_frontier_inclusively(
    evaluated_at: datetime,
) -> None:
    decision = _current_decision(
        retrieved_at=_at(10, 15),
        evaluated_at=evaluated_at,
    )

    assert decision.result == "rejected"
    assert decision.reasons == (
        TemporalEligibilityRejectionReason.REQUESTED_SESSION_NOT_LATEST_COMPLETED_FRONTIER,
    )


@pytest.mark.unit
def test_stale_reuse_after_multiple_completed_sessions_rejects() -> None:
    decision = _current_decision(
        retrieved_at=_at(10, 15),
        evaluated_at=_at(13, 15),
    )

    assert decision.result == "rejected"
    assert decision.calendar_binding.latest_completed_session == date(2026, 8, 13)


@pytest.mark.unit
@pytest.mark.parametrize(
    ("changes", "expected_reason"),
    (
        (
            {"observed_at": _at(10, 15, 1), "retrieved_at": _at(10, 15)},
            "observation_after_retrieval",
        ),
        (
            {
                "evidence_effective_as_of": None,
                "evidence_effective_as_of_derivation": None,
            },
            "evidence_effective_as_of_unproven",
        ),
        (
            {"evidence_effective_as_of": date(2026, 8, 11)},
            "evidence_effective_after_requested_session",
        ),
        ({"ann_date": None, "ann_date_derivation": None}, "ann_date_missing"),
        (
            {"ann_date": date(2026, 8, 11)},
            "ann_date_after_requested_session",
        ),
    ),
)
def test_common_provider_proof_failures_are_typed(
    changes: dict[str, object],
    expected_reason: str,
) -> None:
    evidence = _evidence(**{"retrieved_at": _at(10, 15), **changes})
    decision = _current_decision(
        retrieved_at=evidence.retrieved_at,
        evaluated_at=_at(10, 15, 5),
        evidence=evidence,
    )

    assert decision.result == "rejected"
    assert expected_reason in tuple(reason.value for reason in decision.reasons)


@pytest.mark.unit
def test_current_analysis_rejects_before_close_and_after_fixed_evaluation() -> None:
    before_close = _current_decision(
        retrieved_at=_at(10, 14, 59),
        evaluated_at=_at(10, 15, 5),
    )
    after_evaluation = _current_decision(
        retrieved_at=_at(10, 15, 6),
        evaluated_at=_at(10, 15, 5),
    )

    assert "retrieval_before_requested_session_completed" in {
        reason.value for reason in before_close.reasons
    }
    assert after_evaluation.reasons == (
        TemporalEligibilityRejectionReason.RETRIEVAL_AFTER_ELIGIBILITY_EVALUATION,
    )


def _strict_decision(
    evidence: FinancialMetadataTemporalEvidence,
    *,
    cutoff: datetime | None = _STRICT_CUTOFF,
):
    binding = build_market_session_eligibility_binding(
        _calendar(),
        requested_session=date(2026, 8, 10),
        eligibility_evaluated_at=_at(13, 15),
    )
    return evaluate_financial_metadata_temporal_eligibility(
        mode=TemporalEligibilityMode.STRICT_HISTORICAL_PIT,
        evidence=evidence,
        calendar_binding=binding,
        strict_cutoff=cutoff,
    )


@pytest.mark.unit
def test_strict_pit_direct_pre_cutoff_control_accepts() -> None:
    decision = _strict_decision(_evidence(retrieved_at=_at(10, 14)))

    assert decision.result == "accepted"
    assert decision.strict_cutoff == _at(10, 15)


@pytest.mark.unit
def test_strict_pit_requires_eligible_first_announcement_date() -> None:
    missing = _strict_decision(
        _evidence(
            retrieved_at=_at(10, 14),
            f_ann_date=None,
            f_ann_date_derivation=None,
        )
    )
    later = _strict_decision(_evidence(retrieved_at=_at(10, 14), f_ann_date=date(2026, 8, 11)))

    assert "f_ann_date_missing_for_strict_pit" in {reason.value for reason in missing.reasons}
    assert "f_ann_date_after_requested_session" in {reason.value for reason in later.reasons}


@pytest.mark.unit
def test_post_cutoff_retrieval_requires_qualified_exact_revision_proof() -> None:
    no_proof = _strict_decision(_evidence(retrieved_at=_at(11, 0, 48)))
    exact = ExactRevisionAvailabilityProof(
        exact_revision_authoritative_available_at=_at(10, 14),
        authoritative=True,
        scope=ExactRevisionAvailabilityScope.EXACT_IMMUTABLE_REVISION,
        proof_identity="exchange-revision-proof:v1:" + "d" * 64,
        artifact=_artifact(),
    )
    accepted = _strict_decision(
        _evidence(
            retrieved_at=_at(11, 0, 48),
            exact_revision_availability_proof=exact,
        )
    )
    generic = _strict_decision(
        _evidence(
            retrieved_at=_at(11, 0, 48),
            exact_revision_availability_proof=exact.model_copy(
                update={"scope": ExactRevisionAvailabilityScope.GENERIC_FILING}
            ),
        )
    )

    assert "exact_revision_not_available_by_strict_cutoff" in {
        reason.value for reason in no_proof.reasons
    }
    assert accepted.result == "accepted"
    assert generic.result == "rejected"
    assert generic.reasons == (
        TemporalEligibilityRejectionReason.EXACT_REVISION_NOT_AVAILABLE_BY_STRICT_CUTOFF,
    )


@pytest.mark.unit
def test_exact_temporal_proof_must_bind_the_evaluated_immutable_revision() -> None:
    other = _artifact().model_copy(update={"revision_identity": "revision:other"})
    evidence = _evidence(
        retrieved_at=_at(10, 15),
        ann_date_derivation=ProviderPublicationDateDerivation(
            date_kind=PublicationDateKind.ANN_DATE,
            semantics=(ProviderPublicationDateSemantics.TUSHARE_STATEMENT_PUBLICATION_FIELDS),
            source_field="ann_date",
            artifact=other,
        ),
    )
    decision = _current_decision(
        retrieved_at=evidence.retrieved_at,
        evaluated_at=_at(10, 15, 5),
        evidence=evidence,
    )

    assert decision.reasons == (
        TemporalEligibilityRejectionReason.TEMPORAL_EVIDENCE_ARTIFACT_BINDING_MISMATCH,
    )


@pytest.mark.unit
def test_requested_session_and_diagnostic_fields_cannot_supply_provider_proof() -> None:
    payload = _evidence(retrieved_at=_at(10, 15)).model_dump(mode="python")
    payload["evidence_effective_as_of"] = None
    payload["evidence_effective_as_of_derivation"] = None
    payload["retrieval_local_date"] = date(2026, 8, 10)
    evidence = FinancialMetadataTemporalEvidence.model_validate(payload)
    decision = _current_decision(
        retrieved_at=evidence.retrieved_at,
        evaluated_at=_at(10, 15, 5),
        evidence=evidence,
    )

    assert "evidence_effective_as_of_unproven" in {reason.value for reason in decision.reasons}
    with pytest.raises(ValidationError):
        FinancialMetadataTemporalEvidence.model_validate(
            {**payload, "requested_session": date(2026, 8, 10)}
        )

    request_derived_announcement = _evidence(
        retrieved_at=_at(10, 15),
        ann_date=date(2026, 8, 10),
        ann_date_derivation=None,
    )
    request_derived_decision = _current_decision(
        retrieved_at=request_derived_announcement.retrieved_at,
        evaluated_at=_at(10, 15, 5),
        evidence=request_derived_announcement,
    )
    assert request_derived_decision.result == "rejected"
    assert (
        TemporalEligibilityRejectionReason.TEMPORAL_EVIDENCE_ARTIFACT_BINDING_MISMATCH
        in request_derived_decision.reasons
    )


@pytest.mark.unit
def test_retrieval_local_date_has_zero_eligibility_or_identity_authority() -> None:
    base = _evidence(retrieved_at=_at(11, 0, 48), retrieval_local_date=date(1999, 1, 1))
    other = base.model_copy(update={"retrieval_local_date": date(2099, 1, 1)})
    left = _current_decision(
        retrieved_at=base.retrieved_at,
        evaluated_at=_at(11, 0, 49),
        evidence=base,
    )
    right = _current_decision(
        retrieved_at=other.retrieved_at,
        evaluated_at=_at(11, 0, 49),
        evidence=other,
    )

    assert left == right


@pytest.mark.unit
def test_unknown_mode_and_closed_reason_compatibility_fail_closed() -> None:
    binding = build_market_session_eligibility_binding(
        _calendar(),
        requested_session=date(2026, 8, 10),
        eligibility_evaluated_at=_at(10, 15, 5),
    )
    decision = evaluate_financial_metadata_temporal_eligibility(
        mode="future_mode",
        evidence=_evidence(retrieved_at=_at(10, 15)),
        calendar_binding=binding,
    )

    assert decision.result == "rejected"
    assert decision.reasons == (
        TemporalEligibilityRejectionReason.UNKNOWN_TEMPORAL_ELIGIBILITY_MODE,
    )
    projection = decision.model_dump(mode="python")
    projection["reasons"] = ("future_reason",)
    with pytest.raises(ValidationError):
        FinancialMetadataTemporalEligibilityDecision.model_validate(projection)

    other_unknown = evaluate_financial_metadata_temporal_eligibility(
        mode="other!",
        evidence=_evidence(retrieved_at=_at(10, 15)),
        calendar_binding=binding,
    )
    malformed_unknown = evaluate_financial_metadata_temporal_eligibility(
        mode="bad!",
        evidence=_evidence(retrieved_at=_at(10, 15)),
        calendar_binding=binding,
    )
    assert other_unknown.decision_identity != malformed_unknown.decision_identity


@pytest.mark.unit
def test_recomputed_identity_cannot_admit_a_contradictory_decision() -> None:
    decision = _current_decision(
        retrieved_at=_at(10, 15),
        evaluated_at=_at(10, 15, 5),
    )
    projection = decision.model_dump(mode="python")
    projection["observed_at"] = _at(10, 15, 1)
    identity_payload = {
        key: value for key, value in projection.items() if key != "decision_identity"
    }
    projection["decision_identity"] = temporal_module._identity(
        temporal_module._DECISION_IDENTITY_PREFIX,
        identity_payload,
    )

    with pytest.raises(ValidationError, match="predicate mismatch"):
        FinancialMetadataTemporalEligibilityDecision.model_validate(projection)


@pytest.mark.unit
def test_calendar_mismatch_and_closed_requested_session_are_typed() -> None:
    valid = build_market_session_eligibility_binding(
        _calendar(),
        requested_session=date(2026, 8, 10),
        eligibility_evaluated_at=_at(10, 15, 5),
    )
    mismatched = valid.model_copy(
        update={
            "calendar_revision_id": "market-session-calendar:v1:" + "e" * 64
        }
    )
    mismatch_decision = evaluate_financial_metadata_temporal_eligibility(
        mode=TemporalEligibilityMode.CURRENT_ANALYSIS_ONLY,
        evidence=_evidence(retrieved_at=_at(10, 15)),
        calendar_binding=mismatched,
    )
    closed = build_market_session_eligibility_binding(
        _calendar(),
        requested_session=date(2026, 8, 12),
        eligibility_evaluated_at=_at(12, 15),
    )
    closed_decision = evaluate_financial_metadata_temporal_eligibility(
        mode=TemporalEligibilityMode.CURRENT_ANALYSIS_ONLY,
        evidence=_evidence(retrieved_at=_at(11, 15)),
        calendar_binding=closed,
    )

    assert "calendar_eligibility_binding_mismatch" in {
        reason.value for reason in mismatch_decision.reasons
    }
    assert "requested_session_not_open_in_pinned_calendar" in {
        reason.value for reason in closed_decision.reasons
    }


@pytest.mark.unit
def test_reason_order_and_decision_identity_are_deterministic() -> None:
    evidence = _evidence(
        retrieved_at=_at(10, 14, 59),
        observed_at=_at(10, 15),
        evidence_effective_as_of=None,
        evidence_effective_as_of_derivation=None,
        ann_date=None,
        ann_date_derivation=None,
    )
    binding = build_market_session_eligibility_binding(
        _calendar(),
        requested_session=date(2026, 8, 10),
        eligibility_evaluated_at=_at(10, 15, 5),
    )
    first = evaluate_financial_metadata_temporal_eligibility(
        mode="current_analysis_only",
        evidence=evidence,
        calendar_binding=binding,
    )
    evidence_reordered = FinancialMetadataTemporalEvidence.model_validate(
        dict(reversed(tuple(evidence.model_dump(mode="python").items())))
    )
    binding_reordered = type(binding).model_validate(
        dict(reversed(tuple(binding.model_dump(mode="python").items())))
    )
    second = evaluate_financial_metadata_temporal_eligibility(
        mode="current_analysis_only",
        evidence=evidence_reordered,
        calendar_binding=binding_reordered,
    )

    assert tuple(reason.value for reason in first.reasons) == tuple(
        sorted(reason.value for reason in first.reasons)
    )
    assert len(first.reasons) == len(set(first.reasons))
    assert first == second


@pytest.mark.unit
def test_every_material_temporal_input_changes_the_decision_identity() -> None:
    base_evidence = _evidence(retrieved_at=_at(10, 15))
    base_binding = build_market_session_eligibility_binding(
        _calendar(),
        requested_session=date(2026, 8, 10),
        eligibility_evaluated_at=_at(10, 15, 5),
    )
    base = evaluate_financial_metadata_temporal_eligibility(
        mode=TemporalEligibilityMode.CURRENT_ANALYSIS_ONLY,
        evidence=base_evidence,
        calendar_binding=base_binding,
    )
    mutations = (
        base_evidence.model_copy(
            update={
                "evidence_class": (
                    TemporalEvidenceClass.AKSHARE_SINA_STATEMENT_COMPANY_TYPE
                )
            }
        ),
        base_evidence.model_copy(update={"ann_date": date(2026, 8, 6)}),
        base_evidence.model_copy(update={"evidence_effective_as_of": date(2026, 6, 29)}),
        base_evidence.model_copy(update={"f_ann_date": date(2026, 8, 6)}),
        base_evidence.model_copy(update={"observed_at": _at(10, 14, 59)}),
        base_evidence.model_copy(update={"retrieved_at": _at(10, 15, 1)}),
        base_evidence.model_copy(
            update={
                "evidence_effective_as_of_derivation": (
                    base_evidence.evidence_effective_as_of_derivation.model_copy(
                        update={"source_field": "period_end"}
                    )
                )
            }
        ),
        base_evidence.model_copy(
            update={
                "ann_date_derivation": base_evidence.ann_date_derivation.model_copy(
                    update={"source_field": "publish_date"}
                )
            }
        ),
        base_evidence.model_copy(
            update={
                "f_ann_date_derivation": (
                    base_evidence.f_ann_date_derivation.model_copy(
                        update={
                            "semantics": (
                                ProviderPublicationDateSemantics.AKSHARE_SINA_STATEMENT_PUBLICATION_FIELDS
                            )
                        }
                    )
                )
            }
        ),
        base_evidence.model_copy(
            update={
                "artifact": base_evidence.artifact.model_copy(
                    update={"artifact_identity": "artifact:alternate"}
                )
            }
        ),
        base_evidence.model_copy(
            update={
                "artifact": base_evidence.artifact.model_copy(
                    update={"revision_identity": "revision:alternate"}
                )
            }
        ),
        base_evidence.model_copy(
            update={
                "artifact": base_evidence.artifact.model_copy(
                    update={"row_identity": "row:company-type:alternate"}
                )
            }
        ),
    )
    identities = {
        evaluate_financial_metadata_temporal_eligibility(
            mode=TemporalEligibilityMode.CURRENT_ANALYSIS_ONLY,
            evidence=mutation,
            calendar_binding=base_binding,
        ).decision_identity
        for mutation in mutations
    }
    changed_calendar = build_market_session_eligibility_binding(
        _calendar(),
        requested_session=date(2026, 8, 10),
        eligibility_evaluated_at=_at(10, 15, 6),
    )
    identities.add(
        evaluate_financial_metadata_temporal_eligibility(
            mode=TemporalEligibilityMode.CURRENT_ANALYSIS_ONLY,
            evidence=base_evidence,
            calendar_binding=changed_calendar,
        ).decision_identity
    )

    assert base.decision_identity not in identities
    assert len(identities) == len(mutations) + 1


@pytest.mark.unit
def test_strict_cutoff_and_exact_revision_fields_are_identity_material() -> None:
    proof = ExactRevisionAvailabilityProof(
        exact_revision_authoritative_available_at=_at(10, 14),
        authoritative=True,
        scope=ExactRevisionAvailabilityScope.EXACT_IMMUTABLE_REVISION,
        proof_identity="exchange-revision-proof:v1:" + "d" * 64,
        artifact=_artifact(),
    )
    evidence = _evidence(
        retrieved_at=_at(11, 0, 48),
        exact_revision_availability_proof=proof,
    )
    base = _strict_decision(evidence)
    mutations = (
        (proof.model_copy(update={"authoritative": False}), _STRICT_CUTOFF),
        (
            proof.model_copy(
                update={"scope": ExactRevisionAvailabilityScope.GENERIC_FILING}
            ),
            _STRICT_CUTOFF,
        ),
        (
            proof.model_copy(
                update={
                    "exact_revision_authoritative_available_at": _at(10, 13, 59)
                }
            ),
            _STRICT_CUTOFF,
        ),
        (
            proof.model_copy(update={"proof_identity": "proof:alternate"}),
            _STRICT_CUTOFF,
        ),
        (proof, _at(10, 14, 59)),
    )
    identities = {
        _strict_decision(
            evidence.model_copy(
                update={"exact_revision_availability_proof": changed_proof}
            ),
            cutoff=cutoff,
        ).decision_identity
        for changed_proof, cutoff in mutations
    }

    assert base.result == "accepted"
    assert base.decision_identity not in identities
    assert len(identities) == len(mutations)


@pytest.mark.unit
def test_temporal_evaluator_has_no_io_or_clock_dependency() -> None:
    assert tuple(signature(evaluate_financial_metadata_temporal_eligibility).parameters) == (
        "mode",
        "evidence",
        "calendar_binding",
        "strict_cutoff",
    )


@pytest.mark.unit
def test_rejection_reason_vocabulary_is_exact_and_closed() -> None:
    assert {reason.value for reason in TemporalEligibilityRejectionReason} == {
        "requested_session_not_open_in_pinned_calendar",
        "calendar_eligibility_binding_mismatch",
        "observation_after_retrieval",
        "evidence_effective_as_of_unproven",
        "evidence_effective_after_requested_session",
        "ann_date_missing",
        "ann_date_after_requested_session",
        "retrieval_before_requested_session_completed",
        "retrieval_after_eligibility_evaluation",
        "requested_session_not_latest_completed_frontier",
        "f_ann_date_missing_for_strict_pit",
        "f_ann_date_after_requested_session",
        "exact_revision_not_available_by_strict_cutoff",
        "temporal_evidence_artifact_binding_mismatch",
        "unknown_temporal_eligibility_mode",
    }

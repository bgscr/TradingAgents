"""Publication-aware mainland financial period-target construction."""

from __future__ import annotations

import json
from collections.abc import Sequence
from datetime import date
from hashlib import sha256

from tradingagents.dataflows.financial_contracts import (
    FinancialCompanyType,
    FinancialConsolidationScope,
    FinancialListingProvenance,
    FinancialPeriodCandidate,
)
from tradingagents.dataflows.financial_eligibility_policy import (
    MAINLAND_FINANCIAL_TARGET_CONSTRUCTION_V2,
    MainlandFinancialEligibilityPolicy,
    MainlandFinancialListingProvenanceBinding,
    MainlandFinancialPeriodTargetConstruction,
    MainlandFinancialPeriodTargetFailureReason,
    MainlandFinancialPublicationCandidateBinding,
    MainlandFinancialPublicationEvidenceBinding,
    MainlandFinancialPublicationPeriod,
    MainlandFinancialPublicationState,
    MainlandFinancialSinceListingTargetException,
    build_mainland_financial_eligibility_policy_v2,
)
from tradingagents.evidence import InstrumentIdentityEvidence

_STANDARD_PERIOD_MONTH_DAYS = ((3, 31), (6, 30), (9, 30), (12, 31))
MAINLAND_FINANCIAL_ANNUAL_TARGET_COUNT = 5
MAINLAND_FINANCIAL_REPORTING_TARGET_COUNT = 8


def _identity_payload(identity: InstrumentIdentityEvidence) -> tuple[object, ...]:
    provenance = identity.provenance
    return (
        identity.symbol,
        identity.venue,
        identity.instrument_kind.value,
        identity.currency,
        (
            None
            if provenance is None
            else (
                provenance.provider,
                provenance.source_ref,
                provenance.retrieved_at,
                provenance.artifact_sha256,
            )
        ),
    )


def _is_standard_period_end(value: date) -> bool:
    return (value.month, value.day) in _STANDARD_PERIOD_MONTH_DAYS


def _latest_standard_reporting_end(as_of_date: date) -> date | None:
    candidates = (
        date(as_of_date.year, month, day)
        for month, day in _STANDARD_PERIOD_MONTH_DAYS
    )
    return max((item for item in candidates if item <= as_of_date), default=None)


def _previous_reporting_end(value: date) -> date:
    index = _STANDARD_PERIOD_MONTH_DAYS.index((value.month, value.day))
    if index:
        month, day = _STANDARD_PERIOD_MONTH_DAYS[index - 1]
        return date(value.year, month, day)
    month, day = _STANDARD_PERIOD_MONTH_DAYS[-1]
    return date(value.year - 1, month, day)


def _reporting_window(frontier: date, count: int) -> tuple[date, ...]:
    periods = [frontier]
    while len(periods) < count:
        periods.append(_previous_reporting_end(periods[-1]))
    return tuple(periods)


def _reporting_periods_since(
    *,
    listing_date: date,
    frontier: date,
) -> tuple[date, ...]:
    periods: list[date] = []
    cursor = frontier
    while cursor >= listing_date:
        periods.append(cursor)
        cursor = _previous_reporting_end(cursor)
    return tuple(periods)


def _calendar_period(year: int, period_end: str) -> date:
    month, day = (int(item) for item in period_end.split("-"))
    return date(year, month, day)


def derive_mainland_financial_acquisition_period_supersets(
    *,
    run_as_of_date: date,
    listing_date: date,
    policy: MainlandFinancialEligibilityPolicy | None = None,
) -> tuple[tuple[date, ...], tuple[date, ...]]:
    """Derive the bounded v2 acquisition horizons before publication filtering."""

    effective_policy = policy or build_mainland_financial_eligibility_policy_v2()
    if listing_date > run_as_of_date:
        raise ValueError("financial listing date follows the run as-of date")
    annual_period_ends = effective_policy.reporting_calendar.annual_period_ends
    annual_candidates = tuple(
        _calendar_period(run_as_of_date.year, period_end)
        for period_end in annual_period_ends
    )
    annual_frontier = max(
        (item for item in annual_candidates if item <= run_as_of_date),
        default=max(
            _calendar_period(run_as_of_date.year - 1, period_end)
            for period_end in annual_period_ends
        ),
    )
    annual = tuple(
        period
        for offset in range(MAINLAND_FINANCIAL_ANNUAL_TARGET_COUNT)
        for period in (date(annual_frontier.year - offset, 12, 31),)
        if period >= listing_date
    )
    reporting_period_ends = effective_policy.reporting_calendar.reporting_period_ends
    reporting_candidates = tuple(
        _calendar_period(run_as_of_date.year, period_end)
        for period_end in reporting_period_ends
    )
    reporting_frontier = max(
        (item for item in reporting_candidates if item <= run_as_of_date),
        default=max(
            _calendar_period(run_as_of_date.year - 1, period_end)
            for period_end in reporting_period_ends
        ),
    )
    reporting_periods: list[date] = []
    cursor = reporting_frontier
    while (
        len(reporting_periods) < MAINLAND_FINANCIAL_REPORTING_TARGET_COUNT
        and cursor >= listing_date
    ):
        reporting_periods.append(cursor)
        cursor = _previous_reporting_end(cursor)
    return annual, tuple(reporting_periods)


def _deadline_for(
    period_end: date,
    policy: MainlandFinancialEligibilityPolicy,
) -> date:
    period_key = period_end.strftime("%m-%d")
    rule = next(
        item
        for item in policy.reporting_calendar.publication_deadlines
        if item.period_end == period_key
    )
    deadline_month, deadline_day = (int(item) for item in rule.deadline.split("-"))
    return date(
        period_end.year + rule.deadline_year_offset,
        deadline_month,
        deadline_day,
    )


def _publication_state_for_period(
    *,
    period_end: date,
    period_candidates: Sequence[FinancialPeriodCandidate],
    run_as_of_date: date,
    authoritative_listing: bool,
    listing_date: date | None,
    policy: MainlandFinancialEligibilityPolicy,
) -> tuple[MainlandFinancialPublicationState, tuple[date, ...]]:
    valid_dates = tuple(
        sorted(
            {
                candidate.filing_metadata.ann_date
                for candidate in period_candidates
                if candidate.filing_metadata.ann_date is not None
            }
        )
    )
    if authoritative_listing and listing_date is not None and period_end < listing_date:
        state = MainlandFinancialPublicationState.PRE_LISTING
    elif any(ann_date <= run_as_of_date for ann_date in valid_dates):
        state = MainlandFinancialPublicationState.PUBLISHED_ELIGIBLE
    elif valid_dates:
        state = MainlandFinancialPublicationState.ANNOUNCED_AFTER_CUTOFF
    elif _deadline_for(period_end, policy) >= run_as_of_date:
        state = MainlandFinancialPublicationState.NOT_YET_ELIGIBLE
    else:
        state = MainlandFinancialPublicationState.PUBLICATION_OVERDUE_UNPROVEN
    return state, valid_dates


def _compatible_candidate(
    candidate: FinancialPeriodCandidate,
    *,
    expected_instrument_identity: InstrumentIdentityEvidence,
    expected_company_type: FinancialCompanyType,
    expected_consolidation_scope: FinancialConsolidationScope,
    qualified_provider_ids: frozenset[str],
) -> bool:
    identity = candidate.period_identity
    resolution = candidate.company_type_resolution
    return bool(
        expected_instrument_identity.is_authoritative
        and identity.instrument_identity.is_authoritative
        and _identity_payload(identity.instrument_identity)
        == _identity_payload(expected_instrument_identity)
        and identity.company_type is expected_company_type
        and resolution.company_type is expected_company_type
        and not resolution.ambiguous
        and not resolution.contradictory
        and identity.consolidation_scope is expected_consolidation_scope
        and _is_standard_period_end(identity.period_end)
        and candidate.artifact.dataset.provider_id in qualified_provider_ids
    )


def filter_mainland_financial_acquisition_periods(
    *,
    period_ends: Sequence[date],
    run_as_of_date: date,
    expected_instrument_identity: InstrumentIdentityEvidence,
    expected_company_type: FinancialCompanyType,
    expected_consolidation_scope: FinancialConsolidationScope,
    candidates: Sequence[FinancialPeriodCandidate],
    qualified_provider_ids: Sequence[str],
    listing_date: date | None = None,
    listing_provenance: FinancialListingProvenance | None = None,
    policy: MainlandFinancialEligibilityPolicy | None = None,
) -> tuple[date, ...]:
    """Exclude only periods already proven ineligible from later fallback gaps."""

    effective_policy = policy or build_mainland_financial_eligibility_policy_v2()
    qualified = frozenset(qualified_provider_ids)
    compatible = tuple(
        candidate
        for candidate in candidates
        if _compatible_candidate(
            candidate,
            expected_instrument_identity=expected_instrument_identity,
            expected_company_type=expected_company_type,
            expected_consolidation_scope=expected_consolidation_scope,
            qualified_provider_ids=qualified,
        )
    )
    by_period: dict[date, list[FinancialPeriodCandidate]] = {}
    for candidate in compatible:
        by_period.setdefault(candidate.period_identity.period_end, []).append(candidate)
    authoritative_listing = listing_date is not None and listing_provenance is not None
    required: list[date] = []
    for period_end in sorted(set(period_ends), reverse=True):
        if not _is_standard_period_end(period_end):
            raise ValueError("mainland acquisition horizon contains a non-standard period")
        if (
            authoritative_listing
            and listing_date is not None
            and period_end < listing_date
        ):
            continue
        state, _ = _publication_state_for_period(
            period_end=period_end,
            period_candidates=by_period.get(period_end, ()),
            run_as_of_date=run_as_of_date,
            authoritative_listing=authoritative_listing,
            listing_date=listing_date,
            policy=effective_policy,
        )
        if state in {
            MainlandFinancialPublicationState.PUBLISHED_ELIGIBLE,
            MainlandFinancialPublicationState.PUBLICATION_OVERDUE_UNPROVEN,
        }:
            required.append(period_end)
    return tuple(required)


def _construction_identity(payload: dict[str, object]) -> str:
    encoded = json.dumps(
        payload,
        allow_nan=False,
        default=lambda value: value.isoformat()
        if isinstance(value, date)
        else str(value),
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
    return (
        "mainland-financial-target-construction:v2:" + sha256(encoded).hexdigest()
    )


def construct_mainland_financial_period_targets(
    *,
    run_as_of_date: date,
    expected_instrument_identity: InstrumentIdentityEvidence,
    expected_company_type: FinancialCompanyType,
    expected_consolidation_scope: FinancialConsolidationScope,
    candidates: Sequence[FinancialPeriodCandidate],
    qualified_provider_ids: Sequence[str],
    acquisition_annual_period_ends: Sequence[date],
    acquisition_reporting_period_ends: Sequence[date],
    listing_date: date | None = None,
    listing_provenance: FinancialListingProvenance | None = None,
    policy: MainlandFinancialEligibilityPolicy | None = None,
) -> MainlandFinancialPeriodTargetConstruction:
    """Build deterministic targets solely from already-retained candidates."""

    effective_policy = policy or build_mainland_financial_eligibility_policy_v2()
    authoritative_listing = (
        listing_date is not None and listing_provenance is not None
    )
    listing_metadata_invalid = (listing_date is None) != (
        listing_provenance is None
    )
    qualified = frozenset(qualified_provider_ids)
    compatible = tuple(
        sorted(
            (
                candidate
                for candidate in candidates
                if _compatible_candidate(
                    candidate,
                    expected_instrument_identity=expected_instrument_identity,
                    expected_company_type=expected_company_type,
                    expected_consolidation_scope=expected_consolidation_scope,
                    qualified_provider_ids=qualified,
                )
            ),
            key=lambda item: item.candidate_identity,
        )
    )
    candidates_by_period: dict[date, list[FinancialPeriodCandidate]] = {}
    for candidate in compatible:
        candidates_by_period.setdefault(candidate.period_identity.period_end, []).append(
            candidate
        )

    supplied_periods = {
        *acquisition_annual_period_ends,
        *acquisition_reporting_period_ends,
        *(candidate.period_identity.period_end for candidate in compatible),
    }
    if any(not _is_standard_period_end(period) for period in supplied_periods):
        raise ValueError("mainland acquisition horizon contains a non-standard period")
    latest_reporting = _latest_standard_reporting_end(run_as_of_date)
    if latest_reporting is not None:
        supplied_periods.add(latest_reporting)
    latest_annual = date(run_as_of_date.year, 12, 31)
    if latest_annual > run_as_of_date:
        latest_annual = date(run_as_of_date.year - 1, 12, 31)
    supplied_periods.add(latest_annual)

    if supplied_periods:
        earliest = min(supplied_periods)
        cursor = latest_reporting
        while cursor is not None and cursor >= earliest:
            supplied_periods.add(cursor)
            cursor = _previous_reporting_end(cursor)

    def classify(period_end: date) -> MainlandFinancialPublicationPeriod:
        period_candidates = candidates_by_period.get(period_end, [])
        state, valid_dates = _publication_state_for_period(
            period_end=period_end,
            period_candidates=period_candidates,
            run_as_of_date=run_as_of_date,
            authoritative_listing=authoritative_listing,
            listing_date=listing_date,
            policy=effective_policy,
        )
        return MainlandFinancialPublicationPeriod(
            period_end=period_end,
            filing_deadline=_deadline_for(period_end, effective_policy),
            publication_state=state,
            sequence_memberships=(
                ("annual", "reporting")
                if period_end.month == 12
                else ("reporting",)
            ),
            compatible_candidate_identities=tuple(
                sorted(candidate.candidate_identity for candidate in period_candidates)
            ),
            compatible_candidate_bindings=tuple(
                sorted(
                    (
                        MainlandFinancialPublicationCandidateBinding(
                            candidate_identity=candidate.candidate_identity,
                            artifact_identity=candidate.artifact.artifact_identity,
                            provider_id=candidate.artifact.dataset.provider_id,
                            ann_date=candidate.filing_metadata.ann_date,
                        )
                        for candidate in period_candidates
                    ),
                    key=lambda item: item.candidate_identity,
                )
            ),
            valid_announcement_dates=valid_dates,
        )

    initial_periods = tuple(
        classify(period) for period in sorted(supplied_periods, reverse=True)
    )
    annual_frontier = max(
        (
            item.period_end
            for item in initial_periods
            if "annual" in item.sequence_memberships
            and item.publication_state
            is MainlandFinancialPublicationState.PUBLISHED_ELIGIBLE
        ),
        default=None,
    )
    reporting_frontier = max(
        (
            item.period_end
            for item in initial_periods
            if item.publication_state
            is MainlandFinancialPublicationState.PUBLISHED_ELIGIBLE
        ),
        default=None,
    )
    annual_targets = (
        tuple(
            date(annual_frontier.year - offset, 12, 31)
            for offset in range(MAINLAND_FINANCIAL_ANNUAL_TARGET_COUNT)
        )
        if annual_frontier is not None
        else ()
    )
    reporting_targets = (
        _reporting_window(
            reporting_frontier,
            MAINLAND_FINANCIAL_REPORTING_TARGET_COUNT,
        )
        if reporting_frontier is not None
        else ()
    )
    listing_provenance_binding = (
        MainlandFinancialListingProvenanceBinding(
            authoritative=True,
            provider_id=listing_provenance.provider_id,
            source_ref=listing_provenance.source_ref,
            observed_at=listing_provenance.observed_at,
        )
        if listing_provenance is not None
        else None
    )
    since_listing_exception: MainlandFinancialSinceListingTargetException | None = None
    if (
        authoritative_listing
        and listing_date is not None
        and listing_provenance is not None
        and annual_frontier is not None
        and reporting_frontier is not None
    ):
        post_listing_annual = tuple(
            date(year, 12, 31)
            for year in range(annual_frontier.year, listing_date.year - 1, -1)
            if date(year, 12, 31) >= listing_date
        )
        post_listing_reporting = _reporting_periods_since(
            listing_date=listing_date,
            frontier=reporting_frontier,
        )
        annual_age_short = (
            len(post_listing_annual) < MAINLAND_FINANCIAL_ANNUAL_TARGET_COUNT
        )
        reporting_age_short = (
            len(post_listing_reporting) < MAINLAND_FINANCIAL_REPORTING_TARGET_COUNT
        )
        if annual_age_short or reporting_age_short:
            annual_targets = (
                post_listing_annual if annual_age_short else annual_targets
            )
            reporting_targets = (
                post_listing_reporting if reporting_age_short else reporting_targets
            )
    all_period_ends = supplied_periods | set(annual_targets) | set(reporting_targets)
    publication_periods = tuple(
        classify(period) for period in sorted(all_period_ends, reverse=True)
    )
    period_by_end = {item.period_end: item for item in publication_periods}
    missing_annual = tuple(
        period
        for period in annual_targets
        if period_by_end[period].publication_state
        is not MainlandFinancialPublicationState.PUBLISHED_ELIGIBLE
    )
    missing_reporting = tuple(
        period
        for period in reporting_targets
        if period_by_end[period].publication_state
        is not MainlandFinancialPublicationState.PUBLISHED_ELIGIBLE
    )
    if authoritative_listing and listing_date is not None and listing_provenance is not None:
        post_listing_annual_count = (
            len(
                tuple(
                    date(year, 12, 31)
                    for year in range(
                        annual_frontier.year,
                        listing_date.year - 1,
                        -1,
                    )
                    if date(year, 12, 31) >= listing_date
                )
            )
            if annual_frontier is not None
            else 0
        )
        post_listing_reporting_count = (
            len(
                _reporting_periods_since(
                    listing_date=listing_date,
                    frontier=reporting_frontier,
                )
            )
            if reporting_frontier is not None
            else 0
        )
        exception_applicable = (
            annual_frontier is not None
            and reporting_frontier is not None
            and (
                post_listing_annual_count < MAINLAND_FINANCIAL_ANNUAL_TARGET_COUNT
                or post_listing_reporting_count
                < MAINLAND_FINANCIAL_REPORTING_TARGET_COUNT
            )
        )
        if exception_applicable:
            pre_listing_periods = tuple(
                item.period_end
                for item in publication_periods
                if item.publication_state is MainlandFinancialPublicationState.PRE_LISTING
            )
            since_listing_exception = MainlandFinancialSinceListingTargetException(
                listing_date=listing_date,
                listing_provenance=listing_provenance_binding,
                annual_publication_frontier=annual_frontier,
                reporting_publication_frontier=reporting_frontier,
                annual_target_period_ends=annual_targets,
                reporting_target_period_ends=reporting_targets,
                missing_annual_period_ends=missing_annual,
                missing_reporting_period_ends=missing_reporting,
                pre_listing_period_ends=pre_listing_periods,
                supported=not missing_annual and not missing_reporting,
            )
    failure_reason: MainlandFinancialPeriodTargetFailureReason | None = None
    if listing_metadata_invalid:
        failure_reason = (
            MainlandFinancialPeriodTargetFailureReason.LISTING_METADATA_NOT_AUTHORITATIVE
        )
    elif annual_frontier is None or reporting_frontier is None:
        failure_reason = MainlandFinancialPeriodTargetFailureReason.NO_PUBLICATION_FRONTIER
    elif since_listing_exception is not None and not since_listing_exception.supported:
        failure_reason = (
            MainlandFinancialPeriodTargetFailureReason.UNSUPPORTED_SINCE_LISTING_COVERAGE
        )
    elif missing_annual or missing_reporting or any(
        item.publication_state
        is MainlandFinancialPublicationState.PUBLICATION_OVERDUE_UNPROVEN
        and (
            item.period_end > annual_frontier
            if "annual" in item.sequence_memberships
            else item.period_end > reporting_frontier
        )
        for item in publication_periods
    ):
        failure_reason = (
            MainlandFinancialPeriodTargetFailureReason.PUBLICATION_METADATA_UNPROVEN
        )

    bindings = tuple(
        sorted(
            (
                MainlandFinancialPublicationEvidenceBinding(
                    period_end=candidate.period_identity.period_end,
                    ann_date=candidate.filing_metadata.ann_date,
                    candidate_identity=candidate.candidate_identity,
                    artifact_identity=candidate.artifact.artifact_identity,
                    provider_id=candidate.artifact.dataset.provider_id,
                )
                for candidate in compatible
                if candidate.filing_metadata.ann_date is not None
                and candidate.filing_metadata.ann_date <= run_as_of_date
                and (
                    not authoritative_listing
                    or listing_date is None
                    or candidate.period_identity.period_end >= listing_date
                )
            ),
            key=lambda item: (
                item.period_end,
                item.ann_date,
                item.candidate_identity,
            ),
            reverse=True,
        )
    )
    target_set = set(annual_targets) | set(reporting_targets)
    excluded = tuple(
        item for item in publication_periods if item.period_end not in target_set
    )
    payload: dict[str, object] = {
        "contract_version": MAINLAND_FINANCIAL_TARGET_CONSTRUCTION_V2,
        "eligibility_policy_version": effective_policy.eligibility_policy_version,
        "run_as_of_date": run_as_of_date,
        "listing_date": listing_date,
        "listing_provenance": listing_provenance_binding,
        "annual_publication_frontier": annual_frontier,
        "reporting_publication_frontier": reporting_frontier,
        "annual_target_period_ends": annual_targets,
        "reporting_target_period_ends": reporting_targets,
        "publication_periods": publication_periods,
        "excluded_periods": excluded,
        "publication_evidence_bindings": bindings,
        "since_listing_exception": since_listing_exception,
        "failure_reason": failure_reason,
        "evidence_integrity_status": (
            "Decision-Ready" if failure_reason is None else "Insufficient"
        ),
    }
    serialized_payload = {
        key: (
            value.model_dump(mode="json")
            if hasattr(value, "model_dump")
            else [
                item.model_dump(mode="json") if hasattr(item, "model_dump") else item
                for item in value
            ]
            if isinstance(value, tuple)
            else value.value
            if hasattr(value, "value")
            else value
        )
        for key, value in payload.items()
    }
    return MainlandFinancialPeriodTargetConstruction(
        **payload,
        construction_identity=_construction_identity(serialized_payload),
    )


__all__ = [
    "construct_mainland_financial_period_targets",
    "derive_mainland_financial_acquisition_period_supersets",
    "filter_mainland_financial_acquisition_periods",
]

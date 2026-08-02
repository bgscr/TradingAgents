from __future__ import annotations

import copy
import importlib.util
import json
from datetime import date, datetime, timedelta, timezone
from hashlib import sha256

import pytest

from tradingagents.asset_configuration import resolve_run_asset_configuration
from tradingagents.capability_routing import preflight_mainland_capability_routing
from tradingagents.dataflows.financial_capability_routing import (
    FinancialIndicatorRoutingRequest,
    FinancialProviderPeriodResponse,
    FinancialStatementRoutingRequest,
    MainlandFinancialCapabilityRouter,
    render_indicator_routing_result,
    render_statement_routing_result,
)
from tradingagents.dataflows.financial_contracts import (
    FinancialCapability,
    FinancialCompanyType,
    FinancialConsolidationScope,
    FinancialFilingMetadata,
    FinancialListingProvenance,
    FinancialPeriodCandidate,
    FinancialPeriodDisposition,
    FinancialPeriodIdentity,
    FinancialPeriodRejectionReason,
    FinancialProviderArtifactIdentity,
    FinancialProviderDatasetIdentity,
    FinancialRatioFamily,
    FinancialReportingFrequency,
    FinancialStatementType,
    assess_financial_period_candidate,
    resolve_financial_company_type,
)
from tradingagents.dataflows.financial_dispatch import (
    FinancialProvider,
    FinancialProviderVariant,
    FinancialToolDispatcher,
    FinancialToolRequest,
    project_financial_dispatch_ledger,
)
from tradingagents.dataflows.financial_eligibility_policy import (
    MainlandFinancialPeriodTargetConstruction,
    MainlandFinancialPeriodTargetFailureReason,
    MainlandFinancialPublicationState,
)
from tradingagents.dataflows.financial_period_targets import (
    construct_mainland_financial_period_targets,
)
from tradingagents.default_config import DEFAULT_CONFIG
from tradingagents.evidence import (
    AcquisitionUnavailableReason,
    IdentityProvenance,
    InstrumentIdentityEvidence,
    InstrumentKind,
)

_AS_OF = date(2026, 8, 1)
_OBSERVED_AT = datetime(2026, 8, 1, 12, tzinfo=timezone.utc)
_ANNUAL_TARGETS = tuple(date(year, 12, 31) for year in range(2025, 2020, -1))
_REPORTING_TARGETS = (
    date(2026, 3, 31),
    date(2025, 12, 31),
    date(2025, 9, 30),
    date(2025, 6, 30),
    date(2025, 3, 31),
    date(2024, 12, 31),
    date(2024, 9, 30),
    date(2024, 6, 30),
)


def _identity(*, symbol: str = "600895.SS") -> InstrumentIdentityEvidence:
    return InstrumentIdentityEvidence(
        symbol=symbol,
        venue="XSHG",
        instrument_kind=InstrumentKind.EQUITY,
        currency="CNY",
        provenance=IdentityProvenance(
            provider="fixture-registry",
            source_ref="registry:mainland:v1",
            retrieved_at="2026-08-01T00:00:00Z",
            artifact_sha256="a" * 64,
        ),
    )


def _candidate(
    period_end: date,
    *,
    ann_date: date,
    frequency: FinancialReportingFrequency,
    provider_id: str = "fixture_provider",
    instrument_identity: InstrumentIdentityEvidence | None = None,
    company_type: FinancialCompanyType = FinancialCompanyType.INDUSTRIAL_NON_BANK,
    consolidation_scope: FinancialConsolidationScope = (
        FinancialConsolidationScope.CONSOLIDATED
    ),
) -> FinancialPeriodCandidate:
    identity = instrument_identity or _identity()
    dataset = FinancialProviderDatasetIdentity.create(
        provider_id=provider_id,
        endpoint_id="balancesheet",
        dataset_id="single_stock_history",
        schema_identity=f"{provider_id}-publication-fixture-v1",
    )
    artifact = FinancialProviderArtifactIdentity.create(
        dataset=dataset,
        canonical_request={
            "symbol": identity.symbol,
            "period_end": period_end.isoformat(),
            "frequency": frequency.value,
        },
        provider_metadata={"report_type": "1", "comp_type": "1"},
        retrieved_at=_OBSERVED_AT,
        observed_at=_OBSERVED_AT,
        payload={"period_end": period_end.isoformat()},
    )
    return FinancialPeriodCandidate.create(
        artifact=artifact,
        period_identity=FinancialPeriodIdentity.create(
            instrument_identity=identity,
            capability=FinancialCapability.STATEMENT,
            statement_type=FinancialStatementType.BALANCE_SHEET,
            ratio_family=None,
            period_end=period_end,
            frequency=frequency,
            company_type=company_type,
            consolidation_scope=consolidation_scope,
            currency="CNY",
            provider_revision_binding=artifact.artifact_identity,
        ),
        filing_metadata=FinancialFilingMetadata(
            ann_date=ann_date,
            f_ann_date=None,
            report_type="1",
            comp_type="1",
            update_flag=None,
            retrieved_at=_OBSERVED_AT,
            observed_at=_OBSERVED_AT,
            local_provider_revision_identity=artifact.artifact_identity,
        ),
        company_type_resolution=resolve_financial_company_type(
            provider_declared_type=company_type,
            provider_declaration_qualified=True,
            classifier_metadata={},
            present_fields=(),
        ),
        fields=(),
    )


def _established_candidates(
    *,
    provider_id: str = "fixture_provider",
    instrument_identity: InstrumentIdentityEvidence | None = None,
) -> tuple[FinancialPeriodCandidate, ...]:
    annual = tuple(
        _candidate(
            period,
            ann_date=date(period.year + 1, 3, 31),
            frequency=FinancialReportingFrequency.ANNUAL,
            provider_id=provider_id,
            instrument_identity=instrument_identity,
        )
        for period in _ANNUAL_TARGETS
    )
    reporting = tuple(
        _candidate(
            period,
            ann_date=(
                date(period.year + 1, 3, 31)
                if period.month == 12
                else date(period.year, period.month + 1, 20)
            ),
            frequency=FinancialReportingFrequency.QUARTERLY,
            provider_id=provider_id,
            instrument_identity=instrument_identity,
        )
        for period in _REPORTING_TARGETS
    )
    return (*annual, *reporting)


def test_2026_08_01_targets_are_publication_aware_and_exact() -> None:
    result = construct_mainland_financial_period_targets(
        run_as_of_date=_AS_OF,
        expected_instrument_identity=_identity(),
        expected_company_type=FinancialCompanyType.INDUSTRIAL_NON_BANK,
        expected_consolidation_scope=FinancialConsolidationScope.CONSOLIDATED,
        candidates=_established_candidates(),
        qualified_provider_ids=("fixture_provider",),
        acquisition_annual_period_ends=_ANNUAL_TARGETS,
        acquisition_reporting_period_ends=(date(2026, 6, 30), *_REPORTING_TARGETS),
    )

    assert result.eligibility_policy_version == (
        "mainland-financial-period-eligibility-v2"
    )
    assert result.annual_publication_frontier == date(2025, 12, 31)
    assert result.reporting_publication_frontier == date(2026, 3, 31)
    assert result.annual_target_period_ends == _ANNUAL_TARGETS
    assert result.reporting_target_period_ends == _REPORTING_TARGETS
    assert result.failure_reason is None
    assert result.evidence_integrity_status == "Decision-Ready"
    q2 = next(
        period
        for period in result.excluded_periods
        if period.period_end == date(2026, 6, 30)
    )
    assert q2.publication_state is MainlandFinancialPublicationState.NOT_YET_ELIGIBLE
    assert date(2026, 6, 30) not in result.annual_target_period_ends
    assert date(2026, 6, 30) not in result.reporting_target_period_ends


def _construct(
    *,
    as_of: date = _AS_OF,
    candidates: tuple[FinancialPeriodCandidate, ...] | None = None,
    annual_periods: tuple[date, ...] = _ANNUAL_TARGETS,
    reporting_periods: tuple[date, ...] = (
        date(2026, 6, 30),
        *_REPORTING_TARGETS,
    ),
    qualified_provider_ids: tuple[str, ...] = ("fixture_provider",),
    listing_date: date | None = None,
    listing_provenance: FinancialListingProvenance | None = None,
):
    return construct_mainland_financial_period_targets(
        run_as_of_date=as_of,
        expected_instrument_identity=_identity(),
        expected_company_type=FinancialCompanyType.INDUSTRIAL_NON_BANK,
        expected_consolidation_scope=FinancialConsolidationScope.CONSOLIDATED,
        candidates=_established_candidates() if candidates is None else candidates,
        qualified_provider_ids=qualified_provider_ids,
        acquisition_annual_period_ends=annual_periods,
        acquisition_reporting_period_ends=reporting_periods,
        listing_date=listing_date,
        listing_provenance=listing_provenance,
    )


def _period_state(result, period_end: date) -> MainlandFinancialPublicationState:
    return next(
        period.publication_state
        for period in result.publication_periods
        if period.period_end == period_end
    )


def _rehashed_construction_payload(
    result: MainlandFinancialPeriodTargetConstruction,
    **updates: object,
) -> dict[str, object]:
    payload = result.model_dump(mode="json")
    payload.update(updates)
    payload.pop("construction_identity")
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
    payload["construction_identity"] = (
        "mainland-financial-target-construction:v2:"
        + sha256(encoded).hexdigest()
    )
    return payload


def test_announcement_cutoff_is_inclusive_and_advances_only_reporting_frontier() -> None:
    announced_q2 = _candidate(
        date(2026, 6, 30),
        ann_date=date(2026, 8, 15),
        frequency=FinancialReportingFrequency.QUARTERLY,
    )
    candidates = (*_established_candidates(), announced_q2)

    before = _construct(as_of=date(2026, 8, 14), candidates=candidates)
    on_announcement = _construct(as_of=date(2026, 8, 15), candidates=candidates)

    assert _period_state(before, date(2026, 6, 30)) is (
        MainlandFinancialPublicationState.ANNOUNCED_AFTER_CUTOFF
    )
    assert before.reporting_publication_frontier == date(2026, 3, 31)
    assert _period_state(on_announcement, date(2026, 6, 30)) is (
        MainlandFinancialPublicationState.PUBLISHED_ELIGIBLE
    )
    assert on_announcement.reporting_publication_frontier == date(2026, 6, 30)
    assert before.annual_publication_frontier == on_announcement.annual_publication_frontier


def test_deadline_day_absence_is_not_yet_eligible_then_becomes_overdue() -> None:
    candidates = tuple(
        candidate
        for candidate in _established_candidates()
        if candidate.period_identity.period_end != date(2026, 3, 31)
    )

    on_deadline = _construct(as_of=date(2026, 4, 30), candidates=candidates)
    after_deadline = _construct(as_of=date(2026, 5, 1), candidates=candidates)

    assert _period_state(on_deadline, date(2026, 3, 31)) is (
        MainlandFinancialPublicationState.NOT_YET_ELIGIBLE
    )
    assert _period_state(after_deadline, date(2026, 3, 31)) is (
        MainlandFinancialPublicationState.PUBLICATION_OVERDUE_UNPROVEN
    )
    assert after_deadline.failure_reason is (
        MainlandFinancialPeriodTargetFailureReason.PUBLICATION_METADATA_UNPROVEN
    )
    assert after_deadline.evidence_integrity_status == "Insufficient"


@pytest.mark.parametrize(
    ("period_end", "deadline"),
    (
        (date(2025, 12, 31), date(2026, 4, 30)),
        (date(2026, 3, 31), date(2026, 4, 30)),
        (date(2025, 6, 30), date(2025, 8, 31)),
        (date(2025, 9, 30), date(2025, 10, 31)),
    ),
)
def test_every_v2_deadline_is_inclusive(
    period_end: date,
    deadline: date,
) -> None:
    for as_of in (deadline - timedelta(days=1), deadline):
        result = _construct(
            as_of=as_of,
            candidates=(),
            annual_periods=(period_end,) if period_end.month == 12 else (),
            reporting_periods=(period_end,),
        )
        assert _period_state(result, period_end) is (
            MainlandFinancialPublicationState.NOT_YET_ELIGIBLE
        )

    overdue = _construct(
        as_of=deadline + timedelta(days=1),
        candidates=(),
        annual_periods=(period_end,) if period_end.month == 12 else (),
        reporting_periods=(period_end,),
    )
    assert _period_state(overdue, period_end) is (
        MainlandFinancialPublicationState.PUBLICATION_OVERDUE_UNPROVEN
    )


def test_hole_behind_frontier_fails_without_substituting_older_publication() -> None:
    hole = date(2025, 6, 30)
    older = date(2024, 3, 31)
    candidates = (
        *(
            candidate
            for candidate in _established_candidates()
            if candidate.period_identity.period_end != hole
        ),
        _candidate(
            older,
            ann_date=date(2024, 4, 20),
            frequency=FinancialReportingFrequency.QUARTERLY,
        ),
    )

    result = _construct(
        candidates=candidates,
        reporting_periods=(date(2026, 6, 30), *_REPORTING_TARGETS, older),
    )

    assert result.reporting_target_period_ends == _REPORTING_TARGETS
    assert older not in result.reporting_target_period_ends
    assert _period_state(result, hole) is (
        MainlandFinancialPublicationState.PUBLICATION_OVERDUE_UNPROVEN
    )
    assert result.failure_reason is (
        MainlandFinancialPeriodTargetFailureReason.PUBLICATION_METADATA_UNPROVEN
    )


def test_no_publication_frontier_is_typed_insufficient_not_empty_complete_history() -> None:
    result = _construct(candidates=())

    assert result.annual_publication_frontier is None
    assert result.reporting_publication_frontier is None
    assert result.annual_target_period_ends == ()
    assert result.reporting_target_period_ends == ()
    assert result.failure_reason is (
        MainlandFinancialPeriodTargetFailureReason.NO_PUBLICATION_FRONTIER
    )
    assert result.evidence_integrity_status == "Insufficient"


def test_sparse_compatible_candidate_proves_publication_but_not_completeness() -> None:
    sparse = next(
        candidate
        for candidate in _established_candidates()
        if candidate.period_identity.period_end == date(2025, 12, 31)
        and candidate.period_identity.frequency is FinancialReportingFrequency.ANNUAL
    )
    assessment = assess_financial_period_candidate(
        sparse,
        requested_capability=FinancialCapability.STATEMENT,
        requested_statement_type=FinancialStatementType.BALANCE_SHEET,
        requested_ratio_family=None,
        expected_instrument_identity=_identity(),
        expected_frequency=FinancialReportingFrequency.ANNUAL,
        expected_currency="CNY",
        expected_consolidation_scope=FinancialConsolidationScope.CONSOLIDATED,
        pit_as_of_date=_AS_OF,
    )

    result = _construct()

    assert assessment.disposition is FinancialPeriodDisposition.REJECTED
    assert FinancialPeriodRejectionReason.INSUFFICIENT_COMPLETENESS in (
        assessment.rejection_reasons
    )
    assert result.annual_publication_frontier == date(2025, 12, 31)
    assert any(
        binding.candidate_identity == sparse.candidate_identity
        for binding in result.publication_evidence_bindings
    )


def test_incompatible_or_unqualified_candidates_cannot_prove_publication() -> None:
    q2 = date(2026, 6, 30)
    incompatible = (
        _candidate(
            q2,
            ann_date=date(2026, 7, 20),
            frequency=FinancialReportingFrequency.QUARTERLY,
            instrument_identity=_identity(symbol="600000.SS"),
        ),
        _candidate(
            q2,
            ann_date=date(2026, 7, 20),
            frequency=FinancialReportingFrequency.QUARTERLY,
            company_type=FinancialCompanyType.BANK,
        ),
        _candidate(
            q2,
            ann_date=date(2026, 7, 20),
            frequency=FinancialReportingFrequency.QUARTERLY,
            consolidation_scope=FinancialConsolidationScope.PARENT,
        ),
        _candidate(
            q2,
            ann_date=date(2026, 7, 20),
            frequency=FinancialReportingFrequency.QUARTERLY,
            provider_id="unqualified_provider",
        ),
    )

    result = _construct(candidates=(*_established_candidates(), *incompatible))

    assert _period_state(result, q2) is (
        MainlandFinancialPublicationState.NOT_YET_ELIGIBLE
    )
    assert not {
        candidate.candidate_identity for candidate in incompatible
    }.intersection(
        binding.candidate_identity for binding in result.publication_evidence_bindings
    )


def test_candidate_and_provider_order_cannot_change_target_output() -> None:
    candidates = _established_candidates()

    first = _construct(candidates=candidates)
    shuffled = _construct(
        candidates=tuple(reversed(candidates)),
        annual_periods=tuple(reversed(_ANNUAL_TARGETS)),
        reporting_periods=tuple(reversed((date(2026, 6, 30), *_REPORTING_TARGETS))),
        qualified_provider_ids=("unused_provider", "fixture_provider"),
    )

    assert first == shuffled
    assert first.construction_identity == shuffled.construction_identity


def test_target_construction_rejects_rehashed_local_contradictions() -> None:
    result = _construct()
    unknown_candidate_id = "financial-period-candidate:v1:" + "b" * 64
    invalid_binding = result.publication_evidence_bindings[0].model_copy(
        update={"candidate_identity": unknown_candidate_id}
    )
    forged_provenance_binding = result.publication_evidence_bindings[0].model_copy(
        update={
            "artifact_identity": "financial-provider-artifact:v1:" + "c" * 64,
            "provider_id": "forged_provider",
        }
    )
    contradictions = (
        {"annual_publication_frontier": date(2024, 12, 31)},
        {
            "annual_target_period_ends": (
                *result.annual_target_period_ends,
                date(2020, 12, 31),
            )
        },
        {
            "publication_evidence_bindings": (
                invalid_binding.model_dump(mode="json"),
                *(
                    binding.model_dump(mode="json")
                    for binding in result.publication_evidence_bindings[1:]
                ),
            )
        },
        {
            "publication_evidence_bindings": (
                forged_provenance_binding.model_dump(mode="json"),
                *(
                    binding.model_dump(mode="json")
                    for binding in result.publication_evidence_bindings[1:]
                ),
            )
        },
        {
            "failure_reason": "no_publication_frontier",
            "evidence_integrity_status": "Insufficient",
        },
    )

    for updates in contradictions:
        with pytest.raises(ValueError):
            MainlandFinancialPeriodTargetConstruction.model_validate(
                _rehashed_construction_payload(result, **updates)
            )


def _listing_provenance() -> FinancialListingProvenance:
    return FinancialListingProvenance(
        provider_id="fixture_registry",
        source_ref="acq.v1:listing:" + "3" * 64,
        observed_at=_OBSERVED_AT,
    )


def test_authoritative_since_listing_exception_reduces_only_pre_listing_ends() -> None:
    listing_date = date(2024, 6, 15)

    result = _construct(
        listing_date=listing_date,
        listing_provenance=_listing_provenance(),
    )

    exception = result.since_listing_exception
    assert result.failure_reason is None
    assert exception is not None
    assert exception.supported is True
    assert exception.reason == "authoritative_since_listing"
    assert exception.annual_target_period_ends == (
        date(2025, 12, 31),
        date(2024, 12, 31),
    )
    assert exception.reporting_target_period_ends == _REPORTING_TARGETS
    assert result.annual_target_period_ends == exception.annual_target_period_ends
    assert result.reporting_target_period_ends == exception.reporting_target_period_ends
    assert all(period < listing_date for period in exception.pre_listing_period_ends)
    assert all(
        _period_state(result, period) is MainlandFinancialPublicationState.PRE_LISTING
        for period in exception.pre_listing_period_ends
    )
    assert not set(exception.pre_listing_period_ends).intersection(
        binding.period_end for binding in result.publication_evidence_bindings
    )


def test_missing_post_listing_publication_fails_the_exception() -> None:
    listing_date = date(2024, 6, 15)
    missing = date(2025, 9, 30)
    candidates = tuple(
        candidate
        for candidate in _established_candidates()
        if candidate.period_identity.period_end != missing
    )

    result = _construct(
        candidates=candidates,
        listing_date=listing_date,
        listing_provenance=_listing_provenance(),
    )

    assert result.since_listing_exception is not None
    assert result.since_listing_exception.supported is False
    assert result.since_listing_exception.missing_reporting_period_ends == (missing,)
    assert result.failure_reason is (
        MainlandFinancialPeriodTargetFailureReason.UNSUPPORTED_SINCE_LISTING_COVERAGE
    )
    assert result.evidence_integrity_status == "Insufficient"


def test_missing_listing_authority_and_provider_sparsity_cannot_create_exception() -> None:
    listing_date = date(2024, 6, 15)
    post_listing_candidates = tuple(
        candidate
        for candidate in _established_candidates()
        if candidate.period_identity.period_end >= listing_date
    )

    unauthoritative = _construct(
        candidates=post_listing_candidates,
        listing_date=listing_date,
        listing_provenance=None,
    )
    mature_sparse = _construct(
        candidates=post_listing_candidates,
        listing_date=date(2010, 1, 1),
        listing_provenance=_listing_provenance(),
    )

    assert unauthoritative.since_listing_exception is None
    assert unauthoritative.failure_reason is (
        MainlandFinancialPeriodTargetFailureReason.LISTING_METADATA_NOT_AUTHORITATIVE
    )
    assert mature_sparse.since_listing_exception is None
    assert mature_sparse.failure_reason is (
        MainlandFinancialPeriodTargetFailureReason.PUBLICATION_METADATA_UNPROVEN
    )


def _shadow_plan(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(importlib.util, "find_spec", lambda _name: object())
    config = copy.deepcopy(DEFAULT_CONFIG)
    config.update(
        {
            "mainland_capability_routing_mode": "qualified_v1_shadow",
            "tushare_enabled_capabilities": [
                "statements",
                "financial_indicators",
            ],
            "tushare_qualification_profile": "cn-a-2000-20260729-v1",
            "tushare_account_scope_label": "personal-research-primary",
            "tushare_calls_per_minute": 40,
            "tushare_operator_safety_ceiling_calls_per_minute": 40,
        }
    )
    asset = resolve_run_asset_configuration(
        "600895.SS",
        config=copy.deepcopy(DEFAULT_CONFIG),
    )
    preflight = preflight_mainland_capability_routing(
        asset,
        config=config,
        environment={"TUSHARE_TOKEN": "deterministic-fixture-only"},
    )
    assert preflight.plan is not None
    return preflight.plan


def test_v2_router_persists_targets_without_additional_provider_io(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    candidates = _established_candidates(provider_id="tushare")
    retained_artifacts = tuple(
        sorted(
            {candidate.artifact for candidate in candidates},
            key=lambda item: item.artifact_identity,
        )
    )
    provider_calls = 0

    def source(_request):
        nonlocal provider_calls
        provider_calls += 1
        return FinancialProviderPeriodResponse.available(
            provider_id="tushare",
            response_artifact=retained_artifacts[0],
            retained_artifacts=retained_artifacts,
            candidates=candidates,
            subrequest_keys=("fixture:tushare:balancesheet",),
            physical_attempt_ids=(),
        )

    router = MainlandFinancialCapabilityRouter(
        routing_plan=_shadow_plan(monkeypatch),
        statement_sources={"tushare": source},
    )
    request = FinancialStatementRoutingRequest(
        contract_version="financial-statement-routing-request-v2",
        eligibility_policy_version="mainland-financial-period-eligibility-v2",
        instrument_identity=_identity(),
        statement_type=FinancialStatementType.BALANCE_SHEET,
        as_of_date=_AS_OF,
        company_type=FinancialCompanyType.INDUSTRIAL_NON_BANK,
        consolidation_scope=FinancialConsolidationScope.CONSOLIDATED,
        currency="CNY",
        eligible_annual_period_ends=_ANNUAL_TARGETS,
        eligible_reporting_period_ends=(date(2026, 6, 30), *_REPORTING_TARGETS),
    )

    result = router.route_statement(request)

    construction = result.publication_target_construction
    assert provider_calls == 1
    assert construction is not None
    assert result.manifest.contract_version == "financial-manifest-v1"
    assert result.manifest.publication_target_construction is None
    assert json.loads(render_statement_routing_result(result))["contract_version"] == (
        "financial-selected-periods-v2"
    )
    assert construction.annual_target_period_ends == _ANNUAL_TARGETS
    assert construction.reporting_target_period_ends == _REPORTING_TARGETS
    assert result.manifest.aggregate_completeness.target_annual_period_ends == (
        _ANNUAL_TARGETS
    )
    assert result.manifest.aggregate_completeness.target_reporting_period_ends == (
        _REPORTING_TARGETS
    )
    assert date(2026, 6, 30) not in result.missing_annual_period_ends
    assert date(2026, 6, 30) not in result.missing_reporting_period_ends


def _ratio_candidate(
    period_end: date,
    *,
    ann_date: date,
    ratio_family: FinancialRatioFamily = FinancialRatioFamily.PROFIT,
    provider_id: str = "tushare",
) -> FinancialPeriodCandidate:
    identity = _identity()
    dataset = FinancialProviderDatasetIdentity.create(
        provider_id=provider_id,
        endpoint_id="fina_indicator",
        dataset_id="single_stock_history",
        schema_identity=f"{provider_id}-indicator-publication-fixture-v1",
    )
    artifact = FinancialProviderArtifactIdentity.create(
        dataset=dataset,
        canonical_request={
            "symbol": identity.symbol,
            "period_end": period_end.isoformat(),
        },
        provider_metadata={"comp_type": "1"},
        retrieved_at=_OBSERVED_AT,
        observed_at=_OBSERVED_AT,
        payload={"period_end": period_end.isoformat()},
    )
    company_type = FinancialCompanyType.INDUSTRIAL_NON_BANK
    return FinancialPeriodCandidate.create(
        artifact=artifact,
        period_identity=FinancialPeriodIdentity.create(
            instrument_identity=identity,
            capability=FinancialCapability.RATIO_FAMILY,
            statement_type=None,
            ratio_family=ratio_family,
            period_end=period_end,
            frequency=FinancialReportingFrequency.QUARTERLY,
            company_type=company_type,
            consolidation_scope=FinancialConsolidationScope.CONSOLIDATED,
            currency="XXX",
            provider_revision_binding=artifact.artifact_identity,
        ),
        filing_metadata=FinancialFilingMetadata(
            ann_date=ann_date,
            f_ann_date=None,
            report_type=None,
            comp_type="1",
            update_flag=None,
            retrieved_at=_OBSERVED_AT,
            observed_at=_OBSERVED_AT,
            local_provider_revision_identity=artifact.artifact_identity,
        ),
        company_type_resolution=resolve_financial_company_type(
            provider_declared_type=company_type,
            provider_declaration_qualified=True,
            classifier_metadata={},
            present_fields=(),
        ),
        fields=(),
    )


def test_v2_indicator_denominators_use_publication_targets_without_extra_io(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    publication_periods = tuple(
        sorted({*_ANNUAL_TARGETS, *_REPORTING_TARGETS}, reverse=True)
    )
    candidates = tuple(
        _ratio_candidate(
            period,
            ann_date=(
                date(period.year + 1, 3, 31)
                if period.month == 12
                else date(period.year, period.month + 1, 20)
            ),
        )
        for period in publication_periods
    )
    retained_artifacts = tuple(candidate.artifact for candidate in candidates)
    provider_calls = 0

    def source(_request):
        nonlocal provider_calls
        provider_calls += 1
        return FinancialProviderPeriodResponse.available(
            provider_id="tushare",
            response_artifact=retained_artifacts[0],
            retained_artifacts=retained_artifacts,
            candidates=candidates,
            subrequest_keys=("fixture:tushare:fina_indicator",),
            physical_attempt_ids=(),
        )

    router = MainlandFinancialCapabilityRouter(
        routing_plan=_shadow_plan(monkeypatch),
        statement_sources={},
        indicator_sources={"tushare": source},
    )
    result = router.route_indicators(
        FinancialIndicatorRoutingRequest(
            contract_version="financial-indicator-routing-request-v2",
            eligibility_policy_version=(
                "mainland-financial-period-eligibility-v2"
            ),
            instrument_identity=_identity(),
            as_of_date=_AS_OF,
            company_type=FinancialCompanyType.INDUSTRIAL_NON_BANK,
            consolidation_scope=FinancialConsolidationScope.CONSOLIDATED,
            currency="CNY",
            ratio_families=(FinancialRatioFamily.PROFIT,),
            eligible_reporting_period_ends=(
                date(2026, 6, 30),
                *publication_periods,
            ),
        )
    )

    construction = result.publication_target_construction
    assert provider_calls == 1
    assert construction is not None
    assert construction.reporting_target_period_ends == _REPORTING_TARGETS
    assert result.family_manifests[0].contract_version == "financial-manifest-v1"
    assert result.family_manifests[0].publication_target_construction is None
    assert json.loads(render_indicator_routing_result(result))["contract_version"] == (
        "financial-selected-indicators-v2"
    )
    assert result.family_manifests[
        0
    ].aggregate_completeness.target_reporting_period_ends == _REPORTING_TARGETS
    assert all(
        date(2026, 6, 30) not in missing
        for _, missing in result.missing_periods_by_family
    )


def test_indicator_publication_index_uses_retained_unrequested_families(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    q2 = date(2026, 6, 30)
    requested_candidates = tuple(
        _ratio_candidate(
            period,
            ann_date=(
                date(period.year + 1, 3, 31)
                if period.month == 12
                else date(period.year, period.month + 1, 20)
            ),
        )
        for period in sorted({*_ANNUAL_TARGETS, *_REPORTING_TARGETS}, reverse=True)
    )
    unrequested_publication_proof = _ratio_candidate(
        q2,
        ann_date=date(2026, 7, 20),
        ratio_family=FinancialRatioFamily.GROWTH,
    )
    candidates = (*requested_candidates, unrequested_publication_proof)
    retained_artifacts = tuple(candidate.artifact for candidate in candidates)

    def source(_request):
        return FinancialProviderPeriodResponse.available(
            provider_id="tushare",
            response_artifact=retained_artifacts[0],
            retained_artifacts=retained_artifacts,
            candidates=candidates,
            subrequest_keys=("fixture:tushare:issuer-index",),
            physical_attempt_ids=(),
        )

    router = MainlandFinancialCapabilityRouter(
        routing_plan=_shadow_plan(monkeypatch),
        statement_sources={},
        indicator_sources={"tushare": source},
    )
    result = router.route_indicators(
        FinancialIndicatorRoutingRequest(
            contract_version="financial-indicator-routing-request-v2",
            eligibility_policy_version="mainland-financial-period-eligibility-v2",
            instrument_identity=_identity(),
            as_of_date=_AS_OF,
            company_type=FinancialCompanyType.INDUSTRIAL_NON_BANK,
            consolidation_scope=FinancialConsolidationScope.CONSOLIDATED,
            currency="CNY",
            ratio_families=(FinancialRatioFamily.PROFIT,),
            eligible_reporting_period_ends=(q2, *_REPORTING_TARGETS),
        )
    )

    construction = result.publication_target_construction
    assert construction is not None
    assert construction.reporting_publication_frontier == q2
    assert any(
        binding.candidate_identity == unrequested_publication_proof.candidate_identity
        for binding in construction.publication_evidence_bindings
    )


def test_not_yet_eligible_period_never_reaches_fallback_provider(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    candidates = _established_candidates(provider_id="tushare")
    retained_artifacts = tuple(candidate.artifact for candidate in candidates)
    fallback_requests = []

    def primary(_request):
        return FinancialProviderPeriodResponse.available(
            provider_id="tushare",
            response_artifact=retained_artifacts[0],
            retained_artifacts=retained_artifacts,
            candidates=candidates,
            subrequest_keys=("fixture:tushare:publication",),
            physical_attempt_ids=(),
        )

    def fallback(request):
        fallback_requests.append(request)
        return FinancialProviderPeriodResponse.unavailable(
            provider_id="akshare_sina",
            reason=AcquisitionUnavailableReason.NOT_CONFIGURED,
        )

    router = MainlandFinancialCapabilityRouter(
        routing_plan=_shadow_plan(monkeypatch),
        statement_sources={
            "tushare": primary,
            "akshare_sina": fallback,
        },
    )
    result = router.route_statement(
        FinancialStatementRoutingRequest(
            contract_version="financial-statement-routing-request-v2",
            eligibility_policy_version="mainland-financial-period-eligibility-v2",
            instrument_identity=_identity(),
            statement_type=FinancialStatementType.BALANCE_SHEET,
            as_of_date=_AS_OF,
            company_type=FinancialCompanyType.INDUSTRIAL_NON_BANK,
            consolidation_scope=FinancialConsolidationScope.CONSOLIDATED,
            currency="CNY",
            eligible_annual_period_ends=_ANNUAL_TARGETS,
            eligible_reporting_period_ends=(date(2026, 6, 30), *_REPORTING_TARGETS),
        )
    )

    assert len(fallback_requests) == 1
    assert date(2026, 6, 30) not in (
        fallback_requests[0].missing_reporting_period_ends
    )
    assert result.publication_target_construction is not None
    assert result.publication_target_construction.reporting_target_period_ends == (
        _REPORTING_TARGETS
    )


def test_routed_unqualified_artifact_cannot_prove_publication(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    established = _established_candidates(provider_id="tushare")
    q2 = _ratio_candidate(
        date(2026, 6, 30),
        ann_date=date(2026, 7, 20),
        provider_id="yfinance",
    )
    retained_established = tuple(candidate.artifact for candidate in established)

    def primary(_request):
        return FinancialProviderPeriodResponse.available(
            provider_id="tushare",
            response_artifact=retained_established[0],
            retained_artifacts=retained_established,
            candidates=established,
            subrequest_keys=("fixture:tushare:publication",),
            physical_attempt_ids=(),
        )

    def unqualified(_request):
        return FinancialProviderPeriodResponse.available(
            provider_id="yfinance",
            response_artifact=q2.artifact,
            retained_artifacts=(q2.artifact,),
            candidates=(q2,),
            subrequest_keys=("fixture:yfinance:unqualified",),
            physical_attempt_ids=(),
        )

    router = MainlandFinancialCapabilityRouter(
        routing_plan=_shadow_plan(monkeypatch),
        statement_sources={"tushare": primary, "yfinance": unqualified},
    )
    result = router.route_statement(
        FinancialStatementRoutingRequest(
            contract_version="financial-statement-routing-request-v2",
            eligibility_policy_version="mainland-financial-period-eligibility-v2",
            instrument_identity=_identity(),
            statement_type=FinancialStatementType.BALANCE_SHEET,
            as_of_date=_AS_OF,
            company_type=FinancialCompanyType.INDUSTRIAL_NON_BANK,
            consolidation_scope=FinancialConsolidationScope.CONSOLIDATED,
            currency="CNY",
            eligible_annual_period_ends=_ANNUAL_TARGETS,
            eligible_reporting_period_ends=(date(2026, 6, 30), *_REPORTING_TARGETS),
        )
    )

    construction = result.publication_target_construction
    assert construction is not None
    assert _period_state(construction, date(2026, 6, 30)) is (
        MainlandFinancialPublicationState.NOT_YET_ELIGIBLE
    )
    assert all(
        binding.provider_id != "yfinance"
        for binding in construction.publication_evidence_bindings
    )


def test_dispatcher_checkpoint_and_audit_persist_target_construction(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    plan = _shadow_plan(monkeypatch)
    asset = resolve_run_asset_configuration(
        "600895.SS",
        config=copy.deepcopy(DEFAULT_CONFIG),
    )
    candidates = _established_candidates(
        provider_id="tushare",
        instrument_identity=asset.instrument_identity,
    )
    retained_artifacts = tuple(candidate.artifact for candidate in candidates)

    def source(_request):
        return FinancialProviderPeriodResponse.available(
            provider_id="tushare",
            response_artifact=retained_artifacts[0],
            retained_artifacts=retained_artifacts,
            candidates=candidates,
            subrequest_keys=("fixture:tushare:dispatcher",),
            physical_attempt_ids=(),
        )

    router = MainlandFinancialCapabilityRouter(
        routing_plan=plan,
        statement_sources={"tushare": source},
    )
    run_scope_id = "ticket-02-fixture"
    dispatcher = FinancialToolDispatcher(
        instrument_identity=asset.instrument_identity,
        run_asset_configuration=asset,
        provider_chains={
            "get_balance_sheet": (
                FinancialProvider(
                    name="legacy_fixture",
                    variants=(
                        FinancialProviderVariant(
                            variant_id="legacy_fixture",
                            invoke=lambda _request: "unused",
                        ),
                    ),
                ),
            )
        },
        qualified_statement_router=router,
        run_scope_id=run_scope_id,
    )
    selection_request = FinancialStatementRoutingRequest(
        contract_version="financial-statement-routing-request-v2",
        eligibility_policy_version="mainland-financial-period-eligibility-v2",
        instrument_identity=asset.instrument_identity,
        statement_type=FinancialStatementType.BALANCE_SHEET,
        as_of_date=_AS_OF,
        company_type=FinancialCompanyType.INDUSTRIAL_NON_BANK,
        consolidation_scope=FinancialConsolidationScope.CONSOLIDATED,
        currency="CNY",
        eligible_annual_period_ends=_ANNUAL_TARGETS,
        eligible_reporting_period_ends=(date(2026, 6, 30), *_REPORTING_TARGETS),
    )
    result = dispatcher.dispatch_statement_selection(
        FinancialToolRequest(
            tool_name="get_balance_sheet",
            statement_type=FinancialStatementType.BALANCE_SHEET,
            frequency=FinancialReportingFrequency.QUARTERLY,
            as_of_date=_AS_OF,
            tool_call_id="ticket-02-dispatch",
        ),
        selection_request,
    )

    ledger = dispatcher.checkpoint_ledger()
    audit = project_financial_dispatch_ledger(
        ledger,
        run_asset_configuration=asset,
        capability_routing_plan=plan,
        run_scope_id=run_scope_id,
    )

    assert result.routing_result.publication_target_construction is not None
    assert result.routing_result.manifest.contract_version == "financial-manifest-v2"
    assert result.routing_result.manifest.publication_target_construction == (
        result.routing_result.publication_target_construction
    )
    assert json.loads(result.rendered_value)["contract_version"] == (
        "financial-selected-periods-v2"
    )
    assert audit is not None
    projected = audit.manifest_requests[0].manifests[0]
    assert projected.publication_target_construction == (
        result.routing_result.publication_target_construction
    )
    restored = FinancialToolDispatcher(
        instrument_identity=asset.instrument_identity,
        run_asset_configuration=asset,
        provider_chains={
            "get_balance_sheet": (
                FinancialProvider(
                    name="legacy_fixture",
                    variants=(
                        FinancialProviderVariant(
                            variant_id="legacy_fixture",
                            invoke=lambda _request: "unused",
                        ),
                    ),
                ),
            )
        },
        qualified_statement_router=router,
        run_scope_id=run_scope_id,
        checkpoint_ledger=ledger,
    )
    assert restored.checkpoint_ledger() == ledger

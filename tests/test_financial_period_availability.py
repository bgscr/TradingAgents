from __future__ import annotations

import copy
import json
from datetime import date

import pytest

from tests.test_financial_capability_routing_selection import (
    _REPORTING_PERIODS,
    _identity,
    _ratio_candidate,
    _shadow_plan,
)
from tests.test_financial_manifest_evidence_reporting import _attempt_event
from tradingagents.asset_configuration import resolve_run_asset_configuration
from tradingagents.dataflows.financial_capability_routing import (
    FinancialIndicatorRoutingRequest,
    FinancialProviderPeriodResponse,
    MainlandFinancialCapabilityRouter,
    render_indicator_routing_result,
)
from tradingagents.dataflows.financial_contracts import (
    FinancialAvailabilityScope,
    FinancialCapability,
    FinancialCompanyType,
    FinancialConsolidationScope,
    FinancialPeriodDisposition,
    FinancialPeriodRejectionReason,
    FinancialPeriodSelection,
    FinancialRatioFamily,
    FinancialReportingFrequency,
    FinancialStatementType,
    _canonical_digest,
    assess_financial_period_candidate,
)
from tradingagents.dataflows.financial_dispatch import (
    FinancialDispatchCheckpointError,
    FinancialIndicatorSelectionDispatchResult,
    FinancialProvider,
    FinancialProviderVariant,
    FinancialToolDispatcher,
    FinancialToolRequest,
    project_financial_dispatch_ledger,
    project_financial_selection_evidence,
)
from tradingagents.default_config import DEFAULT_CONFIG
from tradingagents.evidence import EvidenceState
from tradingagents.reporting import _render_financial_dispatch_operations

_AS_OF = date(2026, 8, 1)
_ELIGIBILITY_POLICY_VERSION = "mainland-financial-period-eligibility-v2"


def _rehash_v2_selection(payload: dict[str, object]) -> dict[str, object]:
    identity_payload = {
        key: value
        for key, value in payload.items()
        if key != "selection_identity"
    }
    payload["selection_identity"] = (
        "financial-period-selection:v2:" + _canonical_digest(identity_payload)
    )
    return payload


def _indicator_result(
    monkeypatch: pytest.MonkeyPatch,
    *,
    requested_analysis_scope: str = "current_only",
    f_ann_date: date | None = None,
    as_of_date: date = _AS_OF,
):
    request, response, plan = _indicator_fixture(
        monkeypatch,
        requested_analysis_scope=requested_analysis_scope,
        f_ann_date=f_ann_date,
        as_of_date=as_of_date,
    )
    result = MainlandFinancialCapabilityRouter(
        routing_plan=plan,
        statement_sources={},
        indicator_sources={"tushare": lambda _request: response},
    ).route_indicators(request)
    return request, result


def _indicator_fixture(
    monkeypatch: pytest.MonkeyPatch,
    *,
    requested_analysis_scope: str,
    f_ann_date: date | None,
    as_of_date: date,
    instrument_identity=None,
):
    identity = instrument_identity or _identity()
    selected_candidates = tuple(
        _ratio_candidate(
            provider_id="tushare",
            ratio_family=FinancialRatioFamily.PROFIT,
            period_end=period,
            f_ann_date=f_ann_date,
            instrument_identity=identity,
        )
        for period in _REPORTING_PERIODS
    )
    publication_only_candidates = tuple(
        _ratio_candidate(
            provider_id="tushare",
            ratio_family=FinancialRatioFamily.PROFIT,
            period_end=date(year, 12, 31),
            f_ann_date=f_ann_date,
            instrument_identity=identity,
        )
        for year in (2021, 2022, 2023)
    )
    candidates = (*selected_candidates, *publication_only_candidates)
    event = _attempt_event()
    response = FinancialProviderPeriodResponse.available(
        provider_id="tushare",
        response_artifact=candidates[0].artifact,
        retained_artifacts=tuple(candidate.artifact for candidate in candidates),
        candidates=candidates,
        subrequest_keys=(event.request_key,),
        physical_attempt_ids=(event.attempt_event_id,),
        physical_attempt_events=(event,),
    )
    request = FinancialIndicatorRoutingRequest(
        contract_version="financial-indicator-routing-request-v3",
        eligibility_policy_version=_ELIGIBILITY_POLICY_VERSION,
        requested_analysis_scope=requested_analysis_scope,
        instrument_identity=identity,
        as_of_date=as_of_date,
        company_type=FinancialCompanyType.INDUSTRIAL_NON_BANK,
        consolidation_scope=FinancialConsolidationScope.CONSOLIDATED,
        currency="CNY",
        ratio_families=(FinancialRatioFamily.PROFIT,),
        eligible_reporting_period_ends=_REPORTING_PERIODS,
    )
    return request, response, _shadow_plan(monkeypatch)


def test_ticket_03_request_requires_an_explicit_analysis_scope(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    request, _, _ = _indicator_fixture(
        monkeypatch,
        requested_analysis_scope="current_only",
        f_ann_date=None,
        as_of_date=_AS_OF,
    )
    payload = request.model_dump(mode="json")
    payload.pop("requested_analysis_scope")

    with pytest.raises(ValueError, match="requires an explicit availability scope"):
        FinancialIndicatorRoutingRequest.model_validate(payload)

    payload["contract_version"] = "financial-indicator-routing-request-v2"
    ticket_02_request = FinancialIndicatorRoutingRequest.model_validate(payload)
    assert ticket_02_request.requested_analysis_scope is None


def test_complete_announced_indicators_are_explicitly_current_only_and_degraded(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    request, result = _indicator_result(monkeypatch)

    assert result.complete_families == (FinancialRatioFamily.PROFIT,)
    assert result.missing_periods_by_family == ()
    assert len(result.selected_candidates) == 8
    manifest = result.family_manifests[0]
    assert manifest.disposition.value == "degraded"
    assert manifest.requested_analysis_scope.value == "current_only"
    assert manifest.availability_scope.value == "current_only"
    assert manifest.availability_basis.value == (
        "ann_date_without_first_publication"
    )
    assert manifest.eligibility_policy_version == _ELIGIBILITY_POLICY_VERSION
    assert manifest.availability_reason.value == (
        "missing_first_publication_for_strict_pit"
    )

    selections = manifest.selections
    assert len(selections) == 8
    assert all(selection.contract_version == "financial-period-selection-v2" for selection in selections)
    assert all(selection.requested_analysis_scope.value == "current_only" for selection in selections)
    assert all(selection.availability_scope.value == "current_only" for selection in selections)
    assert all(
        selection.availability_basis.value
        == "ann_date_without_first_publication"
        for selection in selections
    )
    assert all(selection.ann_date == date(2026, 7, 1) for selection in selections)
    assert all(selection.f_ann_date is None for selection in selections)
    assert all(
        selection.local_provider_revision_identity == selection.artifact_identity
        for selection in selections
    )
    assert all(selection.observed_at is not None for selection in selections)
    assert all(selection.retrieved_at is not None for selection in selections)
    assert all(selection.ratio_family is FinancialRatioFamily.PROFIT for selection in selections)
    assert {selection.period_end for selection in selections} == set(_REPORTING_PERIODS)
    assert all(
        selection.eligibility_policy_version == _ELIGIBILITY_POLICY_VERSION
        for selection in selections
    )

    rendered = json.loads(render_indicator_routing_result(result))
    assert rendered["requested_analysis_scope"] == "current_only"
    assert rendered["rendering_scope"] == "current_analysis"
    assert rendered["status"] == "current_only_degraded"
    assert all(
        period["availability_scope"] == "current_only"
        and period["availability_reason"]
        == "missing_first_publication_for_strict_pit"
        for period in rendered["periods"]
    )
    assert request.as_of_date.isoformat() in json.dumps(rendered, sort_keys=True)


def test_v2_selection_deserialization_rejects_naive_availability_timestamps(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _, result = _indicator_result(monkeypatch)
    payload = result.family_manifests[0].selections[0].model_dump(mode="json")
    payload["observed_at"] = "2026-08-01T12:00:00"

    with pytest.raises(ValueError, match="availability time must be concrete"):
        FinancialPeriodSelection.model_validate(_rehash_v2_selection(payload))


def test_v2_selection_deserialization_rejects_contradictory_current_only_reason(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _, result = _indicator_result(monkeypatch)
    payload = result.family_manifests[0].selections[0].model_dump(mode="json")
    payload["availability_reason"] = "pit_cutoff_not_satisfied"

    with pytest.raises(ValueError, match="current-only financial availability"):
        FinancialPeriodSelection.model_validate(_rehash_v2_selection(payload))


@pytest.mark.parametrize(
    ("as_of_date", "expected_disposition", "expected_reason"),
    (
        (date(2026, 7, 2), FinancialPeriodDisposition.CURRENT_ONLY, None),
        (date(2026, 7, 1), FinancialPeriodDisposition.CURRENT_ONLY, None),
        (
            date(2026, 6, 30),
            FinancialPeriodDisposition.REJECTED,
            FinancialPeriodRejectionReason.ANNOUNCEMENT_AFTER_CUTOFF,
        ),
    ),
)
def test_current_only_announcement_cutoff_is_inclusive_and_future_safe(
    as_of_date: date,
    expected_disposition: FinancialPeriodDisposition,
    expected_reason: FinancialPeriodRejectionReason | None,
) -> None:
    candidate = _ratio_candidate(
        provider_id="tushare",
        ratio_family=FinancialRatioFamily.PROFIT,
        period_end=date(2026, 3, 31),
        f_ann_date=None,
    )

    assessment = assess_financial_period_candidate(
        candidate,
        requested_capability=FinancialCapability.RATIO_FAMILY,
        requested_statement_type=None,
        requested_ratio_family=FinancialRatioFamily.PROFIT,
        expected_instrument_identity=_identity(),
        expected_frequency=FinancialReportingFrequency.QUARTERLY,
        expected_currency="XXX",
        expected_consolidation_scope=FinancialConsolidationScope.CONSOLIDATED,
        pit_as_of_date=as_of_date,
        ratio_declaration_version="financial-ratio-declarations-v2",
        eligibility_policy_version=_ELIGIBILITY_POLICY_VERSION,
    )

    assert assessment.disposition is expected_disposition
    if expected_reason is None:
        assert FinancialPeriodRejectionReason.ANNOUNCEMENT_AFTER_CUTOFF not in (
            assessment.rejection_reasons
        )
    else:
        assert expected_reason in assessment.rejection_reasons


def test_strict_pit_rejects_current_only_candidates_with_exact_reason(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _request, result = _indicator_result(
        monkeypatch,
        requested_analysis_scope="strict_pit_eligible",
    )

    assert result.selected_candidates == ()
    assert result.complete_families == ()
    manifest = result.family_manifests[0]
    assert manifest.disposition.value == "insufficient"
    assert manifest.requested_analysis_scope.value == "strict_pit_eligible"
    assert manifest.availability_scope.value == "current_only"
    assert manifest.availability_basis.value == (
        "ann_date_without_first_publication"
    )
    assert manifest.availability_reason.value == (
        "missing_first_publication_for_strict_pit"
    )
    assert manifest.selections == ()
    assert manifest.rejected_periods
    assert all(
        rejection.reasons
        == (
            FinancialPeriodRejectionReason.MISSING_FIRST_PUBLICATION_FOR_STRICT_PIT,
        )
        for rejection in manifest.rejected_periods
    )
    assert all(
        item.candidate.filing_metadata.f_ann_date is None
        and item.candidate.filing_metadata.ann_date is not None
        and item.candidate.filing_metadata.update_flag == "0"
        and item.candidate.filing_metadata.retrieved_at is not None
        for item in manifest.provider_candidates
    )

    rendered = json.loads(render_indicator_routing_result(result))
    assert rendered["requested_analysis_scope"] == "strict_pit_eligible"
    assert rendered["rendering_scope"] == "strict_pit"
    assert rendered["status"] == "insufficient_evidence"
    assert rendered["periods"] == []
    assert rendered["availability_reason"] == (
        "missing_first_publication_for_strict_pit"
    )


def test_strict_pit_accepts_first_publication_and_revision_observed_by_cutoff(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _request, result = _indicator_result(
        monkeypatch,
        requested_analysis_scope="strict_pit_eligible",
        f_ann_date=date(2026, 7, 2),
    )

    assert result.complete_families == (FinancialRatioFamily.PROFIT,)
    assert len(result.selected_candidates) == 8
    manifest = result.family_manifests[0]
    assert manifest.disposition.value == "strict_pit_eligible"
    assert manifest.availability_scope.value == "strict_pit_eligible"
    assert manifest.availability_basis.value == (
        "ann_date_first_publication_and_observed_revision"
    )
    assert manifest.availability_reason is None
    assert all(
        selection.requested_analysis_scope.value == "strict_pit_eligible"
        and selection.availability_scope.value == "strict_pit_eligible"
        and selection.f_ann_date == date(2026, 7, 2)
        and selection.observed_at.date() <= _AS_OF
        for selection in manifest.selections
    )


def test_strict_pit_rejects_revision_first_observed_after_cutoff(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _request, result = _indicator_result(
        monkeypatch,
        requested_analysis_scope="strict_pit_eligible",
        f_ann_date=date(2026, 7, 2),
        as_of_date=date(2026, 7, 29),
    )

    assert result.selected_candidates == ()
    assert result.complete_families == ()
    manifest = result.family_manifests[0]
    assert manifest.disposition.value == "insufficient"
    assert manifest.availability_scope.value == "current_only"
    assert manifest.availability_basis.value == (
        "ann_date_with_strict_pit_cutoff_failure"
    )
    assert manifest.availability_reason.value == "pit_cutoff_not_satisfied"
    assert manifest.selections == ()
    assert all(
        FinancialPeriodRejectionReason.PIT_CUTOFF_NOT_SATISFIED
        in rejection.reasons
        for rejection in manifest.rejected_periods
    )


def test_current_only_source_facts_are_explicit_and_cannot_be_consumed_as_strict(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    selection_request, response, plan = _indicator_fixture(
        monkeypatch,
        requested_analysis_scope="current_only",
        f_ann_date=None,
        as_of_date=_AS_OF,
    )
    router = MainlandFinancialCapabilityRouter(
        routing_plan=plan,
        statement_sources={},
        indicator_sources={"tushare": lambda _request: response},
    )
    dispatcher = FinancialToolDispatcher(
        instrument_identity=_identity(),
        provider_chains={
            "get_fundamentals": (
                FinancialProvider(
                    name="unused",
                    variants=(
                        FinancialProviderVariant(
                            variant_id="unused",
                            invoke=lambda _request: "unused",
                        ),
                    ),
                ),
            ),
        },
        qualified_statement_router=router,
        run_scope_id="ticket-03-current-evidence",
    )
    dispatch = dispatcher.dispatch_indicator_selection(
        FinancialToolRequest(
            tool_name="get_fundamentals",
            statement_type=FinancialStatementType.COMPREHENSIVE_FUNDAMENTALS,
            frequency=FinancialReportingFrequency.NOT_APPLICABLE,
            as_of_date=_AS_OF,
            tool_call_id="ticket-03-current-evidence",
        ),
        selection_request,
    )

    evidence = project_financial_selection_evidence(
        EvidenceState(instrument_identity=_identity()),
        dispatch,
    )

    assert evidence.source_facts == ()
    assert evidence.current_analysis_source_facts
    assert all(
        fact.availability_lineage is not None
        and fact.availability_lineage.requested_analysis_scope == "current_only"
        and fact.availability_lineage.availability_scope == "current_only"
        and fact.availability_lineage.f_ann_date is None
        and fact.availability_lineage.availability_reason
        == "missing_first_publication_for_strict_pit"
        and fact.availability_lineage.provider_artifact_identity.startswith(
            "financial-provider-artifact:v1:"
        )
        for fact in evidence.current_analysis_source_facts
    )
    assert all(
        '"rendering_scope":"current_analysis"' in fact.raw_text
        and '"f_ann_date":null' in fact.raw_text
        for fact in evidence.current_analysis_source_facts
    )
    assert evidence.source_facts_for_analysis_scope("current_only") == (
        evidence.current_analysis_source_facts
    )
    assert evidence.source_facts_for_analysis_scope("strict_pit_eligible") == ()
    with pytest.raises(
        ValueError,
        match="cannot enter general Evidence Admission",
    ):
        EvidenceState(source_facts=evidence.current_analysis_source_facts)


def test_strict_checkpoint_replay_performs_zero_io_and_rejects_missing_lineage(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    asset = resolve_run_asset_configuration(
        "600895.SS",
        config=copy.deepcopy(DEFAULT_CONFIG),
    )
    selection_request, response, plan = _indicator_fixture(
        monkeypatch,
        requested_analysis_scope="strict_pit_eligible",
        f_ann_date=None,
        as_of_date=_AS_OF,
        instrument_identity=asset.instrument_identity,
    )
    calls = 0

    def source(_request):
        nonlocal calls
        calls += 1
        return response

    provider_chain = (
        FinancialProvider(
            name="unused",
            variants=(
                FinancialProviderVariant(
                    variant_id="unused",
                    invoke=lambda _request: "unused",
                ),
            ),
        ),
    )
    tool_request = FinancialToolRequest(
        tool_name="get_fundamentals",
        statement_type=FinancialStatementType.COMPREHENSIVE_FUNDAMENTALS,
        frequency=FinancialReportingFrequency.NOT_APPLICABLE,
        as_of_date=_AS_OF,
        tool_call_id="ticket-03-strict-first",
    )
    original = FinancialToolDispatcher(
        instrument_identity=asset.instrument_identity,
        run_asset_configuration=asset,
        provider_chains={"get_fundamentals": provider_chain},
        qualified_statement_router=MainlandFinancialCapabilityRouter(
            routing_plan=plan,
            statement_sources={},
            indicator_sources={"tushare": source},
        ),
        run_scope_id="ticket-03-strict-replay",
    )
    original_result = original.dispatch_indicator_selection(
        tool_request,
        selection_request,
    )
    checkpoint = original.checkpoint_ledger()
    assert calls == 1

    contradictory_result = original_result.routing_result.model_copy(
        update={
            "requested_analysis_scope": FinancialAvailabilityScope.CURRENT_ONLY,
        }
    )
    contradictory_payload = original_result.model_dump(mode="python")
    contradictory_payload["routing_result"] = contradictory_result
    contradictory_payload["rendered_value"] = render_indicator_routing_result(
        contradictory_result
    )
    with pytest.raises(ValueError, match="analysis scope binding"):
        FinancialIndicatorSelectionDispatchResult.model_validate(
            contradictory_payload
        )

    tampered_checkpoint = copy.deepcopy(checkpoint)
    tampered_terminal = tampered_checkpoint["qualified_indicator_selections"][0][
        "terminal"
    ]
    tampered_terminal["routing_result"]["requested_analysis_scope"] = (
        "current_only"
    )
    with pytest.raises(FinancialDispatchCheckpointError):
        FinancialToolDispatcher(
            instrument_identity=asset.instrument_identity,
            run_asset_configuration=asset,
            provider_chains={"get_fundamentals": provider_chain},
            qualified_statement_router=MainlandFinancialCapabilityRouter(
                routing_plan=plan,
                statement_sources={},
                indicator_sources={},
            ),
            run_scope_id="ticket-03-strict-replay",
            checkpoint_ledger=tampered_checkpoint,
        )

    restored = FinancialToolDispatcher(
        instrument_identity=asset.instrument_identity,
        run_asset_configuration=asset,
        provider_chains={"get_fundamentals": provider_chain},
        qualified_statement_router=MainlandFinancialCapabilityRouter(
            routing_plan=plan,
            statement_sources={},
            indicator_sources={
                "tushare": lambda _request: pytest.fail(
                    "strict replay repeated provider I/O"
                )
            },
        ),
        run_scope_id="ticket-03-strict-replay",
        checkpoint_ledger=checkpoint,
    )
    replay = restored.dispatch_indicator_selection(
        tool_request.model_copy(
            update={"tool_call_id": "ticket-03-strict-restored"}
        ),
        selection_request,
    )
    evidence = project_financial_selection_evidence(
        EvidenceState(instrument_identity=asset.instrument_identity),
        replay,
    )

    assert evidence.source_facts == ()
    assert replay.routing_result.selected_candidates == ()
    assert replay.routing_result.family_manifests[0].disposition.value == (
        "insufficient"
    )
    tool_message = replay.to_tool_message()
    assert tool_message.status == "error"
    assert json.loads(tool_message.content)["status"] == "insufficient_evidence"
    serialized = checkpoint
    terminal = serialized["qualified_indicator_selections"][0]["terminal"]
    assert terminal["selection_request"]["requested_analysis_scope"] == (
        "strict_pit_eligible"
    )
    manifest = terminal["routing_result"]["family_manifests"][0]
    assert manifest["availability_scope"] == "current_only"
    assert manifest["availability_reason"] == (
        "missing_first_publication_for_strict_pit"
    )


def test_checkpoint_audit_tool_and_report_agree_on_current_only_availability(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    asset = resolve_run_asset_configuration(
        "600895.SS",
        config=copy.deepcopy(DEFAULT_CONFIG),
    )
    selection_request, response, plan = _indicator_fixture(
        monkeypatch,
        requested_analysis_scope="current_only",
        f_ann_date=None,
        as_of_date=_AS_OF,
        instrument_identity=asset.instrument_identity,
    )
    dispatcher = FinancialToolDispatcher(
        instrument_identity=asset.instrument_identity,
        run_asset_configuration=asset,
        provider_chains={
            "get_fundamentals": (
                FinancialProvider(
                    name="unused",
                    variants=(
                        FinancialProviderVariant(
                            variant_id="unused",
                            invoke=lambda _request: "unused",
                        ),
                    ),
                ),
            ),
        },
        qualified_statement_router=MainlandFinancialCapabilityRouter(
            routing_plan=plan,
            statement_sources={},
            indicator_sources={"tushare": lambda _request: response},
        ),
        run_scope_id="ticket-03-current-projections",
    )
    dispatch = dispatcher.dispatch_indicator_selection(
        FinancialToolRequest(
            tool_name="get_fundamentals",
            statement_type=FinancialStatementType.COMPREHENSIVE_FUNDAMENTALS,
            frequency=FinancialReportingFrequency.NOT_APPLICABLE,
            as_of_date=_AS_OF,
            tool_call_id="ticket-03-current-projections",
        ),
        selection_request,
    )
    checkpoint = dispatcher.checkpoint_ledger()
    audit = project_financial_dispatch_ledger(
        checkpoint,
        run_asset_configuration=asset,
        capability_routing_plan=plan,
        run_scope_id="ticket-03-current-projections",
    )
    assert audit is not None

    checkpoint_manifest = checkpoint["qualified_indicator_selections"][0][
        "terminal"
    ]["routing_result"]["family_manifests"][0]
    audit_manifest = audit.manifest_requests[0].manifests[0]
    tool_envelope = dispatch.to_tool_message().artifact
    tool_manifest = tool_envelope["manifests"][0]
    for projection in (checkpoint_manifest, tool_manifest):
        assert projection["requested_analysis_scope"] == "current_only"
        assert projection["availability_scope"] == "current_only"
        assert projection["availability_basis"] == (
            "ann_date_without_first_publication"
        )
        assert projection["eligibility_policy_version"] == (
            _ELIGIBILITY_POLICY_VERSION
        )
        assert projection["availability_reason"] == (
            "missing_first_publication_for_strict_pit"
        )
    assert audit_manifest.requested_analysis_scope.value == "current_only"
    assert audit_manifest.availability_scope.value == "current_only"
    assert audit_manifest.availability_basis.value == (
        "ann_date_without_first_publication"
    )
    assert audit_manifest.eligibility_policy_version == _ELIGIBILITY_POLICY_VERSION
    assert audit_manifest.availability_reason.value == (
        "missing_first_publication_for_strict_pit"
    )

    assert audit.completeness_rows
    assert all(row.requested_analysis_scope.value == "current_only" for row in audit.completeness_rows)
    assert all(row.availability_scope.value == "current_only" for row in audit.completeness_rows)
    assert all(row.f_ann_date_state == "absent" for row in audit.completeness_rows)
    assert all(row.eligibility_policy_version == _ELIGIBILITY_POLICY_VERSION for row in audit.completeness_rows)
    assert all(
        row.availability_reason
        == "missing_first_publication_for_strict_pit"
        for row in audit.completeness_rows
    )

    report = "\n".join(_render_financial_dispatch_operations(audit))
    for expected in (
        "Requested analysis scope: `current_only`",
        "Availability scope: `current_only`",
        "Availability basis: `ann_date_without_first_publication`",
        "F ann date state",
        "mainland-financial-period-eligibility-v2",
        "missing_first_publication_for_strict_pit",
    ):
        assert expected in report
    bounded = json.dumps(tool_envelope, sort_keys=True).casefold() + report.casefold()
    for forbidden in (
        "raw provider error",
        "payload_body",
        "https://provider.invalid/query?token=secret",
        "credential=secret",
    ):
        assert forbidden not in bounded


def test_strict_source_facts_bind_exact_first_publication_and_revision_lineage(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    selection_request, response, plan = _indicator_fixture(
        monkeypatch,
        requested_analysis_scope="strict_pit_eligible",
        f_ann_date=date(2026, 7, 2),
        as_of_date=_AS_OF,
    )
    dispatcher = FinancialToolDispatcher(
        instrument_identity=_identity(),
        provider_chains={
            "get_fundamentals": (
                FinancialProvider(
                    name="unused",
                    variants=(
                        FinancialProviderVariant(
                            variant_id="unused",
                            invoke=lambda _request: "unused",
                        ),
                    ),
                ),
            ),
        },
        qualified_statement_router=MainlandFinancialCapabilityRouter(
            routing_plan=plan,
            statement_sources={},
            indicator_sources={"tushare": lambda _request: response},
        ),
        run_scope_id="ticket-03-strict-evidence",
    )
    dispatch = dispatcher.dispatch_indicator_selection(
        FinancialToolRequest(
            tool_name="get_fundamentals",
            statement_type=FinancialStatementType.COMPREHENSIVE_FUNDAMENTALS,
            frequency=FinancialReportingFrequency.NOT_APPLICABLE,
            as_of_date=_AS_OF,
            tool_call_id="ticket-03-strict-evidence",
        ),
        selection_request,
    )
    evidence = project_financial_selection_evidence(
        EvidenceState(instrument_identity=_identity()),
        dispatch,
    )

    assert evidence.source_facts
    assert evidence.source_facts_for_analysis_scope("strict_pit_eligible") == (
        evidence.source_facts
    )
    assert all(
        fact.availability_lineage is not None
        and fact.availability_lineage.requested_analysis_scope
        == "strict_pit_eligible"
        and fact.availability_lineage.availability_scope
        == "strict_pit_eligible"
        and fact.availability_lineage.ann_date == date(2026, 7, 1)
        and fact.availability_lineage.f_ann_date == date(2026, 7, 2)
        and fact.availability_lineage.observed_at.date() <= _AS_OF
        and fact.availability_lineage.provider_artifact_identity
        == fact.availability_lineage.local_provider_revision_identity
        for fact in evidence.source_facts
    )

from __future__ import annotations

import copy
import json
from hashlib import sha256

import pytest

from tests.test_financial_period_availability import _AS_OF, _indicator_fixture
from tradingagents.asset_configuration import resolve_run_asset_configuration
from tradingagents.capability_routing import (
    MainlandCapabilityRoutingCheckpointAction,
    MainlandCapabilityRoutingCheckpointError,
    MainlandCapabilityRoutingCheckpointFailureReason,
    MainlandCapabilityRoutingPlan,
    start_new_mainland_capability_routing_run,
    validate_mainland_capability_routing_checkpoint,
)
from tradingagents.dataflows.financial_capability_routing import (
    FinancialIndicatorRoutingResult,
    MainlandFinancialCapabilityRouter,
)
from tradingagents.dataflows.financial_contracts import (
    FinancialReportingFrequency,
    FinancialStatementType,
)
from tradingagents.dataflows.financial_dispatch import (
    FinancialDispatchCheckpointError,
    FinancialDispatchCheckpointFailureReason,
    FinancialProvider,
    FinancialProviderVariant,
    FinancialToolDispatcher,
    FinancialToolRequest,
    project_financial_dispatch_ledger,
    project_financial_selection_evidence,
)
from tradingagents.dataflows.financial_eligibility_policy import (
    MainlandFinancialEligibilityPolicyError,
    MainlandFinancialEligibilityPolicyFailureReason,
    restore_mainland_financial_eligibility_projection_binding,
)
from tradingagents.default_config import DEFAULT_CONFIG
from tradingagents.evidence import EvidenceState
from tradingagents.evidence_artifacts import (
    SourceArtifactManifest,
    project_evidence_for_audit,
)
from tradingagents.graph.propagation import Propagator
from tradingagents.reporting import _render_financial_dispatch_operations


def _run_id(label: str) -> str:
    return "run:" + sha256(label.encode()).hexdigest()


def _provider_chain() -> tuple[FinancialProvider, ...]:
    return (
        FinancialProvider(
            name="legacy-placeholder",
            variants=(
                FinancialProviderVariant(
                    variant_id="default",
                    invoke=lambda _request: pytest.fail(
                        "legacy provider must not run in the qualified fixture"
                    ),
                ),
            ),
        ),
    )


def _ticket06_dispatch(monkeypatch: pytest.MonkeyPatch):
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
    provider_calls: list[str] = []

    def source(_request):
        provider_calls.append("tushare")
        return response

    dispatcher = FinancialToolDispatcher(
        instrument_identity=asset.instrument_identity,
        run_asset_configuration=asset,
        provider_chains={"get_fundamentals": _provider_chain()},
        qualified_statement_router=MainlandFinancialCapabilityRouter(
            routing_plan=plan,
            statement_sources={},
            indicator_sources={"tushare": source},
        ),
        run_scope_id="ticket-06-run",
    )
    tool_request = FinancialToolRequest(
        tool_name="get_fundamentals",
        statement_type=FinancialStatementType.COMPREHENSIVE_FUNDAMENTALS,
        frequency=FinancialReportingFrequency.NOT_APPLICABLE,
        as_of_date=_AS_OF,
        tool_call_id="ticket-06",
    )
    result = dispatcher.dispatch_indicator_selection(
        tool_request,
        selection_request,
    )
    return (
        asset,
        plan,
        selection_request,
        tool_request,
        response,
        dispatcher,
        result,
        provider_calls,
    )


def test_ticket06_version_binding_agrees_across_every_public_projection(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    (
        asset,
        plan,
        _selection_request,
        _tool_request,
        _response,
        dispatcher,
        result,
        provider_calls,
    ) = _ticket06_dispatch(monkeypatch)
    binding = plan.financial_eligibility_binding
    assert binding is not None
    assert binding.model_dump(mode="json") == {
        "contract_version": "mainland-financial-eligibility-binding-v2",
        "eligibility_policy_version": "mainland-financial-period-eligibility-v2",
        "reporting_calendar_deadline_contract_identity": (
            "mainland-financial-reporting-calendar-v2"
        ),
        "target_construction_contract_identity": (
            "mainland-financial-target-construction-v2"
        ),
        "completeness_policy_identity": "mainland-financial-completeness-v2",
        "candidate_selection_contract_identity": (
            "mainland-financial-candidate-selection-v2"
        ),
        "financial_manifest_version": "financial-manifest-v2",
        "checkpoint_projection_version": (
            "mainland-financial-checkpoint-projection-v2"
        ),
        "audit_projection_version": "mainland-financial-audit-projection-v2",
        "report_projection_version": "mainland-financial-report-projection-v2",
        "availability_scope": "mainland-financial-availability-scope-v2",
        "rollout_authority_disposition": "shadow_non_authoritative",
        "qualified_v1_authority_enabled": False,
        "policy_signature": plan.financial_eligibility_policy.policy_signature,
    }

    manifests = result.routing_result.family_manifests
    checkpoint = dispatcher.checkpoint_ledger()
    audit = project_financial_dispatch_ledger(
        checkpoint,
        run_asset_configuration=asset,
        capability_routing_plan=plan,
        run_scope_id="ticket-06-run",
    )
    assert audit is not None
    tool_envelope = result.to_tool_message().artifact
    evidence = project_financial_selection_evidence(
        EvidenceState(financial_eligibility_binding=binding),
        result,
    )
    initial_state = Propagator().create_initial_state(
        "600895.SS",
        str(_AS_OF),
        evidence_state=EvidenceState(),
        asset_configuration=asset,
        capability_routing_plan=plan,
        run_id=_run_id("ticket-06-agent-state"),
    )

    expected = binding.model_dump(mode="json")
    projections = (
        plan.model_dump(mode="json")["financial_eligibility_binding"],
        result.selection_request.model_dump(mode="json")[
            "financial_eligibility_binding"
        ],
        result.routing_result.model_dump(mode="json")[
            "financial_eligibility_binding"
        ],
        *(manifest.model_dump(mode="json")["financial_eligibility_binding"] for manifest in manifests),
        checkpoint["financial_eligibility_binding"],
        evidence.model_dump(mode="json")["financial_eligibility_binding"],
        initial_state["financial_eligibility_binding"],
        initial_state["evidence_state"]["financial_eligibility_binding"],
        audit.model_dump(mode="json")["financial_eligibility_binding"],
        tool_envelope["financial_eligibility_binding"],
    )
    assert projections
    assert all(projection == expected for projection in projections)
    assert checkpoint["contract_version"] == "1.3"
    assert all(
        manifest.requested_analysis_scope.value == "current_only"
        and manifest.availability_scope.value == "current_only"
        for manifest in manifests
    )
    assert binding.rollout_authority_disposition == "shadow_non_authoritative"
    assert provider_calls == ["tushare"]

    report = "\n".join(_render_financial_dispatch_operations(audit))
    for value in expected.values():
        if isinstance(value, str):
            assert value in report
    target = manifests[0].publication_target_construction
    assert target is not None
    for period in target.annual_target_period_ends:
        assert period.isoformat() in report
    for period in target.reporting_target_period_ends:
        assert period.isoformat() in report
    for period in target.excluded_periods:
        assert period.period_end.isoformat() in report
        assert period.publication_state.value in report


@pytest.mark.parametrize(
    ("mutation", "reason"),
    (
        (
            lambda payload: payload.pop("eligibility_policy_version"),
            MainlandFinancialEligibilityPolicyFailureReason.MISSING_POLICY_IDENTITY,
        ),
        (
            lambda payload: payload.__setitem__(
                "eligibility_policy_version",
                "mainland-financial-period-eligibility-v999",
            ),
            MainlandFinancialEligibilityPolicyFailureReason.UNKNOWN_POLICY_IDENTITY,
        ),
        (
            lambda payload: payload.__setitem__(
                "audit_projection_version",
                "mainland-financial-audit-projection-v999",
            ),
            MainlandFinancialEligibilityPolicyFailureReason.CONTRADICTORY_POLICY_IDENTITY,
        ),
    ),
)
def test_ticket06_binding_missing_unknown_or_contradictory_fails_typed(
    monkeypatch: pytest.MonkeyPatch,
    mutation,
    reason: MainlandFinancialEligibilityPolicyFailureReason,
) -> None:
    plan = _indicator_fixture(
        monkeypatch,
        requested_analysis_scope="current_only",
        f_ann_date=None,
        as_of_date=_AS_OF,
    )[2]
    binding = plan.financial_eligibility_binding
    assert binding is not None
    payload = binding.model_dump(mode="json")
    mutation(payload)

    with pytest.raises(MainlandFinancialEligibilityPolicyError) as caught:
        restore_mainland_financial_eligibility_projection_binding(payload)

    assert caught.value.reason is reason


def test_ticket06_binding_cannot_relabel_pre_v2_manifest_or_result(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    result = _ticket06_dispatch(monkeypatch)[6]
    binding = result.routing_result.financial_eligibility_binding
    assert binding is not None
    manifest_payload = result.routing_result.family_manifests[0].model_dump(
        mode="json"
    )
    for field in (
        "eligibility_policy_version",
        "requested_analysis_scope",
        "availability_scope",
        "availability_basis",
        "availability_reason",
    ):
        manifest_payload.pop(field, None)

    with pytest.raises(ValueError, match="eligibility binding"):
        type(result.routing_result.family_manifests[0]).model_validate(
            manifest_payload
        )

    with pytest.raises(ValueError, match="eligibility binding"):
        FinancialIndicatorRoutingResult(
            route=(),
            provider_calls=(),
            family_manifests=(),
            selected_candidates=(),
            complete_families=(),
            missing_periods_by_family=(),
            retained_artifacts=(),
            physical_attempt_ids=(),
            financial_eligibility_binding=binding,
        )


def test_ticket06_legacy_audit_evidence_bytes_omit_v2_binding() -> None:
    projection = project_evidence_for_audit(
        EvidenceState(),
        SourceArtifactManifest(),
    )

    assert projection.model_dump_json() == (
        '{"contract_version":"1.0","instrument_identity":null,'
        '"market_snapshot":null,"artifact_manifest":'
        '{"contract_version":"1.0","artifacts":[]},'
        '"material_claims":[],"source_facts":[],"claim_validations":[],'
        '"sources":[],"acquisition_outcomes":[],'
        '"physical_attempt_events":[],"physical_attempt_count":0}'
    )


def test_ticket06_tampered_checkpoint_binding_fails_before_provider_io(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    (
        asset,
        plan,
        _selection_request,
        _tool_request,
        _response,
        dispatcher,
        _result,
        provider_calls,
    ) = _ticket06_dispatch(monkeypatch)
    checkpoint = dispatcher.checkpoint_ledger()
    tampered = copy.deepcopy(checkpoint)
    tampered["financial_eligibility_binding"]["report_projection_version"] = (
        "mainland-financial-report-projection-v999"
    )
    before = json.dumps(tampered, sort_keys=True, separators=(",", ":"))

    with pytest.raises(FinancialDispatchCheckpointError) as caught:
        FinancialToolDispatcher(
            instrument_identity=asset.instrument_identity,
            run_asset_configuration=asset,
            provider_chains={"get_fundamentals": _provider_chain()},
            qualified_statement_router=MainlandFinancialCapabilityRouter(
                routing_plan=plan,
                statement_sources={},
                indicator_sources={
                    "tushare": lambda _request: pytest.fail(
                        "tampered checkpoint performed provider I/O"
                    )
                },
            ),
            run_scope_id="ticket-06-run",
            checkpoint_ledger=tampered,
        )

    assert caught.value.reason is (
        FinancialDispatchCheckpointFailureReason.ELIGIBILITY_POLICY_MISMATCH
    )
    assert caught.value.diagnostic_code == "financial_eligibility_policy_mismatch"
    assert json.dumps(tampered, sort_keys=True, separators=(",", ":")) == before
    assert provider_calls == ["tushare"]


def test_ticket06_tampered_plan_or_manifest_identity_fails_before_io(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    (
        asset,
        plan,
        _selection_request,
        _tool_request,
        _response,
        dispatcher,
        _result,
        provider_calls,
    ) = _ticket06_dispatch(monkeypatch)
    tampered_plan = plan.model_dump(mode="json")
    tampered_plan["plan_signature"] = "mainland-routing-plan:v1:" + "f" * 64
    with pytest.raises(MainlandCapabilityRoutingCheckpointError) as plan_error:
        validate_mainland_capability_routing_checkpoint(
            active_plan=plan,
            checkpoint_plan=tampered_plan,
        )
    assert plan_error.value.reason is (
        MainlandCapabilityRoutingCheckpointFailureReason.TAMPERED_PROJECTION
    )

    tampered_manifest = dispatcher.checkpoint_ledger()
    manifest = tampered_manifest["qualified_indicator_selections"][0]["terminal"][
        "routing_result"
    ]["family_manifests"][0]
    manifest["manifest_identity"] = "financial-manifest:v2:" + "f" * 64
    before = json.dumps(tampered_manifest, sort_keys=True, separators=(",", ":"))
    with pytest.raises(FinancialDispatchCheckpointError) as manifest_error:
        FinancialToolDispatcher(
            instrument_identity=asset.instrument_identity,
            run_asset_configuration=asset,
            provider_chains={"get_fundamentals": _provider_chain()},
            qualified_statement_router=MainlandFinancialCapabilityRouter(
                routing_plan=plan,
                statement_sources={},
                indicator_sources={
                    "tushare": lambda _request: pytest.fail(
                        "tampered manifest performed provider I/O"
                    )
                },
            ),
            run_scope_id="ticket-06-run",
            checkpoint_ledger=tampered_manifest,
        )
    assert manifest_error.value.reason is (
        FinancialDispatchCheckpointFailureReason.MANIFEST_INVALID
    )
    assert json.dumps(
        tampered_manifest,
        sort_keys=True,
        separators=(",", ":"),
    ) == before
    assert provider_calls == ["tushare"]


@pytest.mark.parametrize(
    "policy_version",
    (None, "mainland-financial-period-eligibility-v999"),
)
def test_ticket06_corrected_dispatch_rejects_missing_or_unknown_policy_before_io(
    monkeypatch: pytest.MonkeyPatch,
    policy_version: str | None,
) -> None:
    (
        _asset,
        _plan,
        selection_request,
        tool_request,
        _response,
        dispatcher,
        _result,
        provider_calls,
    ) = _ticket06_dispatch(monkeypatch)
    tampered_request = selection_request.model_copy(
        update={"eligibility_policy_version": policy_version}
    )

    with pytest.raises(FinancialDispatchCheckpointError) as caught:
        dispatcher.dispatch_indicator_selection(
            tool_request.model_copy(update={"tool_call_id": "ticket-06-policy-tamper"}),
            tampered_request,
        )

    assert caught.value.reason is (
        FinancialDispatchCheckpointFailureReason.ELIGIBILITY_POLICY_MISMATCH
    )
    assert provider_calls == ["tushare"]


def test_ticket06_checkpoint_round_trip_is_exact_and_performs_zero_io(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    (
        asset,
        plan,
        selection_request,
        tool_request,
        _response,
        dispatcher,
        first,
        provider_calls,
    ) = _ticket06_dispatch(monkeypatch)
    checkpoint = dispatcher.checkpoint_ledger()
    before = json.dumps(checkpoint, sort_keys=True, separators=(",", ":"))
    restored = FinancialToolDispatcher(
        instrument_identity=asset.instrument_identity,
        run_asset_configuration=asset,
        provider_chains={"get_fundamentals": _provider_chain()},
        qualified_statement_router=MainlandFinancialCapabilityRouter(
            routing_plan=plan,
            statement_sources={},
            indicator_sources={
                "tushare": lambda _request: pytest.fail(
                    "checkpoint restore or resume repeated provider I/O"
                )
            },
        ),
        run_scope_id="ticket-06-run",
        checkpoint_ledger=checkpoint,
    )
    resumed = restored.dispatch_indicator_selection(
        tool_request.model_copy(update={"tool_call_id": "ticket-06-resumed"}),
        selection_request,
    )

    assert resumed.routing_result == first.routing_result
    assert resumed.selection_request == first.selection_request
    assert resumed.disposition == "duplicate_suppressed"
    assert json.dumps(checkpoint, sort_keys=True, separators=(",", ":")) == before
    assert restored.checkpoint_ledger()["qualified_indicator_selections"][0][
        "terminal"
    ]["routing_result"] == checkpoint["qualified_indicator_selections"][0][
        "terminal"
    ]["routing_result"]
    assert provider_calls == ["tushare"]


def test_ticket06_cross_run_checkpoint_fails_typed_before_io(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    (
        asset,
        plan,
        _selection_request,
        _tool_request,
        _response,
        dispatcher,
        _result,
        provider_calls,
    ) = _ticket06_dispatch(monkeypatch)
    checkpoint = dispatcher.checkpoint_ledger()
    before = json.dumps(checkpoint, sort_keys=True, separators=(",", ":"))

    with pytest.raises(FinancialDispatchCheckpointError) as caught:
        FinancialToolDispatcher(
            instrument_identity=asset.instrument_identity,
            run_asset_configuration=asset,
            provider_chains={"get_fundamentals": _provider_chain()},
            qualified_statement_router=MainlandFinancialCapabilityRouter(
                routing_plan=plan,
                statement_sources={},
                indicator_sources={
                    "tushare": lambda _request: pytest.fail(
                        "cross-run checkpoint performed provider I/O"
                    )
                },
            ),
            run_scope_id="ticket-06-other-run",
            checkpoint_ledger=checkpoint,
        )

    assert caught.value.reason is (
        FinancialDispatchCheckpointFailureReason.RUN_SCOPE_MISMATCH
    )
    assert json.dumps(checkpoint, sort_keys=True, separators=(",", ":")) == before
    assert provider_calls == ["tushare"]


@pytest.mark.parametrize(
    "historical_mode",
    ("qualified_v1", "qualified_v1_shadow"),
)
def test_ticket06_pre_v2_mismatch_can_explicitly_start_distinct_new_run(
    monkeypatch: pytest.MonkeyPatch,
    historical_mode: str,
) -> None:
    active = _indicator_fixture(
        monkeypatch,
        requested_analysis_scope="current_only",
        f_ann_date=None,
        as_of_date=_AS_OF,
    )[2]
    historical = active.model_dump(mode="json")
    historical["mode"] = historical_mode
    if historical_mode == "qualified_v1":
        historical["manifest_contract_version"] = "financial-manifest-v1"
    historical.pop("eligibility_policy_version")
    historical.pop("financial_eligibility_policy")
    historical.pop("financial_eligibility_binding")
    payload = {
        key: value for key, value in historical.items() if key != "plan_signature"
    }
    encoded = json.dumps(
        payload,
        allow_nan=False,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode()
    historical["plan_signature"] = (
        "mainland-routing-plan:v1:" + sha256(encoded).hexdigest()
    )
    historical_plan = MainlandCapabilityRoutingPlan.model_validate(historical)
    historical_bytes = historical_plan.model_dump_json()

    with pytest.raises(MainlandCapabilityRoutingCheckpointError) as caught:
        validate_mainland_capability_routing_checkpoint(
            active_plan=active,
            checkpoint_plan=historical,
        )
    assert caught.value.reason is (
        MainlandCapabilityRoutingCheckpointFailureReason.FINANCIAL_ELIGIBILITY_POLICY_MISMATCH
    )

    previous_run_id = _run_id("ticket-06-old")
    new_run_id = _run_id("ticket-06-new")
    compatibility = start_new_mainland_capability_routing_run(
        active_plan=active,
        checkpoint_plan=historical,
        previous_run_id=previous_run_id,
        new_run_id=new_run_id,
    )

    assert compatibility.action is MainlandCapabilityRoutingCheckpointAction.START_NEW
    assert compatibility.reason is (
        MainlandCapabilityRoutingCheckpointFailureReason.FINANCIAL_ELIGIBILITY_POLICY_MISMATCH
    )
    assert compatibility.previous_run_id == previous_run_id
    assert compatibility.new_run_id == new_run_id
    assert compatibility.previous_run_id != compatibility.new_run_id
    assert compatibility.provider_io_performed is False
    assert compatibility.checkpoint_rewrite_permitted is False
    assert historical_plan.model_dump_json() == historical_bytes

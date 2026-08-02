from __future__ import annotations

import copy
import json
from datetime import date, datetime, timezone
from hashlib import sha256

import pytest

from tests.test_financial_capability_routing_selection import (
    _REPORTING_PERIODS,
    _identity,
    _shadow_plan,
)
from tests.test_financial_manifest_evidence_reporting import _attempt_event
from tradingagents.asset_configuration import resolve_run_asset_configuration
from tradingagents.dataflows.financial_capability_routing import (
    FinancialIndicatorRoutingRequest,
    FinancialProviderPeriodResponse,
    FinancialStatementRoutingRequest,
    MainlandFinancialCapabilityRouter,
)
from tradingagents.dataflows.financial_contracts import (
    FINANCIAL_RATIO_DECLARATION_V1,
    FINANCIAL_RATIO_DECLARATION_V2,
    FINANCIAL_STATEMENT_DECLARATION_TUSHARE_V2,
    FinancialAcquisitionManifest,
    FinancialAvailabilityScope,
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
    FinancialRevisionSelection,
    FinancialStatementType,
    financial_ratio_field_declaration,
    financial_statement_field_declaration,
    resolve_financial_company_type,
)
from tradingagents.dataflows.financial_dispatch import (
    FinancialProvider,
    FinancialProviderVariant,
    FinancialToolDispatcher,
    FinancialToolRequest,
    project_financial_dispatch_ledger,
    project_financial_selection_evidence,
)
from tradingagents.default_config import DEFAULT_CONFIG
from tradingagents.evidence import EvidenceState, InstrumentIdentityEvidence
from tradingagents.reporting import _render_financial_dispatch_operations

_AS_OF = date(2026, 8, 1)
_PERIOD = date(2026, 3, 31)
_POLICY_VERSION = "mainland-financial-period-eligibility-v2"


def _revision(
    *,
    provider_id: str = "tushare",
    period: date = _PERIOD,
    value: str,
    ann_date: date = date(2026, 7, 1),
    observed_at: datetime = datetime(2026, 7, 1, 12, 0, tzinfo=timezone.utc),
    retrieved_at: datetime = datetime(2026, 7, 1, 12, 0, tzinfo=timezone.utc),
    update_flag: str = "0",
    provider_revision_id: str | None = None,
    provider_revision_sequence: int | None = None,
    provider_revision_sequence_qualified: bool = False,
    field_overrides: dict[str, str] | None = None,
    instrument_identity: InstrumentIdentityEvidence | None = None,
    company_type: FinancialCompanyType = FinancialCompanyType.INDUSTRIAL_NON_BANK,
    frequency: FinancialReportingFrequency = FinancialReportingFrequency.QUARTERLY,
    consolidation_scope: FinancialConsolidationScope = (
        FinancialConsolidationScope.CONSOLIDATED
    ),
    currency: str = "CNY",
    normalized_unit: str | None = None,
) -> FinancialPeriodCandidate:
    identity = instrument_identity or _identity()
    statement_type = FinancialStatementType.BALANCE_SHEET
    declaration = financial_statement_field_declaration(
        company_type,
        statement_type,
        declaration_version=(
            FINANCIAL_STATEMENT_DECLARATION_TUSHARE_V2
            if provider_id == "tushare"
            else "financial-field-declarations-v1"
        ),
    )
    dataset = FinancialProviderDatasetIdentity.create(
        provider_id=provider_id,
        endpoint_id=statement_type.value,
        dataset_id="single_stock_history",
        schema_identity=f"ticket-05-{provider_id}-revision-fixture-v1",
    )
    artifact = FinancialProviderArtifactIdentity.create(
        dataset=dataset,
        canonical_request={
            "symbol": identity.symbol,
            "period_end": period.isoformat(),
            "frequency": frequency.value,
        },
        provider_metadata={
            "ann_date": ann_date.isoformat(),
            "report_type": "1",
            "comp_type": "1",
            "update_flag": update_flag,
            "provider_revision_id": provider_revision_id,
            "provider_revision_sequence": provider_revision_sequence,
        },
        retrieved_at=retrieved_at,
        observed_at=observed_at,
        payload={
            "period_end": period.isoformat(),
            "value": value,
            "field_overrides": field_overrides or {},
        },
    )
    fields = tuple(
        FinancialFieldValue(
            provider_field=field,
            original_value=(field_overrides or {}).get(field, value),
            original_unit=normalized_unit or declaration.normalized_unit,
            normalized_field=field,
            normalized_value=(field_overrides or {}).get(field, value),
            normalized_unit=normalized_unit or declaration.normalized_unit,
        )
        for field in declaration.core_fields
    )
    resolution = resolve_financial_company_type(
        provider_declared_type=company_type,
        provider_declaration_qualified=True,
        classifier_metadata={},
        present_fields=declaration.core_fields,
    )
    return FinancialPeriodCandidate.create(
        artifact=artifact,
        period_identity=FinancialPeriodIdentity.create(
            instrument_identity=identity,
            capability=FinancialCapability.STATEMENT,
            statement_type=statement_type,
            ratio_family=None,
            period_end=period,
            frequency=frequency,
            company_type=company_type,
            consolidation_scope=consolidation_scope,
            currency=currency,
            provider_revision_binding=artifact.artifact_identity,
        ),
        filing_metadata=FinancialFilingMetadata(
            ann_date=ann_date,
            f_ann_date=date(2026, 4, 30),
            report_type="1",
            comp_type="2" if company_type is FinancialCompanyType.BANK else "1",
            update_flag=update_flag,
            retrieved_at=retrieved_at,
            observed_at=observed_at,
            local_provider_revision_identity=artifact.artifact_identity,
            provider_filing_revision_id=provider_revision_id,
            provider_filing_revision_sequence=provider_revision_sequence,
            provider_filing_revision_sequence_qualification=(
                "financial-provider-revision-sequence-qualified-v1"
                if provider_revision_sequence_qualified
                else None
            ),
        ),
        company_type_resolution=resolution,
        fields=fields,
    )


def _route(
    monkeypatch: pytest.MonkeyPatch,
    candidates: tuple[FinancialPeriodCandidate, ...],
):
    response = FinancialProviderPeriodResponse.available(
        provider_id="tushare",
        response_artifact=min(
            (candidate.artifact for candidate in candidates),
            key=lambda artifact: artifact.artifact_identity,
        ),
        retained_artifacts=tuple(
            {
                candidate.artifact.artifact_identity: candidate.artifact
                for candidate in candidates
            }.values()
        ),
        candidates=candidates,
        subrequest_keys=("ticket-05-tushare-bulk",),
        physical_attempt_ids=("ticket-05-tushare-attempt",),
    )
    return MainlandFinancialCapabilityRouter(
        routing_plan=_shadow_plan(monkeypatch),
        statement_sources={"tushare": lambda _request: response},
    ).route_statement(
        FinancialStatementRoutingRequest(
            contract_version="financial-statement-routing-request-v2",
            eligibility_policy_version=_POLICY_VERSION,
            instrument_identity=_identity(),
            statement_type=FinancialStatementType.BALANCE_SHEET,
            as_of_date=_AS_OF,
            company_type=FinancialCompanyType.INDUSTRIAL_NON_BANK,
            consolidation_scope=FinancialConsolidationScope.CONSOLIDATED,
            currency="CNY",
            eligible_annual_period_ends=(),
            eligible_reporting_period_ends=(_PERIOD,),
        )
    )


def _indicator_revision(
    *,
    provider_id: str = "tushare",
    period: date,
    value: str,
    ann_date: date = date(2026, 7, 1),
    f_ann_date: date | None = date(2026, 7, 1),
    observed_at: datetime = datetime(2026, 7, 1, 12, 0, tzinfo=timezone.utc),
    retrieved_at: datetime = datetime(2026, 7, 1, 12, 0, tzinfo=timezone.utc),
    provider_revision_id: str | None = None,
    provider_revision_sequence: int | None = None,
    provider_revision_sequence_qualified: bool = False,
    currency: str | None = None,
) -> FinancialPeriodCandidate:
    identity = _identity()
    family = FinancialRatioFamily.PROFIT
    declaration = financial_ratio_field_declaration(
        FinancialCompanyType.INDUSTRIAL_NON_BANK,
        family,
        declaration_version=(
            FINANCIAL_RATIO_DECLARATION_V2
            if provider_id == "tushare"
            else FINANCIAL_RATIO_DECLARATION_V1
        ),
    )
    dataset = FinancialProviderDatasetIdentity.create(
        provider_id=provider_id,
        endpoint_id="financial_indicators",
        dataset_id="single_stock_history",
        schema_identity=f"ticket-05-{provider_id}-indicator-revision-fixture-v1",
    )
    artifact = FinancialProviderArtifactIdentity.create(
        dataset=dataset,
        canonical_request={
            "symbol": identity.symbol,
            "period_end": period.isoformat(),
            "ratio_family": family.value,
        },
        provider_metadata={
            "ann_date": ann_date.isoformat(),
            "provider_revision_id": provider_revision_id,
            "provider_revision_sequence": provider_revision_sequence,
        },
        retrieved_at=retrieved_at,
        observed_at=observed_at,
        payload={"period": period.isoformat(), "value": value},
    )
    fields = tuple(
        FinancialFieldValue(
            provider_field=field,
            original_value=value,
            original_unit=declaration.normalized_unit,
            normalized_field=field,
            normalized_value=value,
            normalized_unit=declaration.normalized_unit,
        )
        for field in declaration.normalized_fields
    )
    resolution = resolve_financial_company_type(
        provider_declared_type=FinancialCompanyType.INDUSTRIAL_NON_BANK,
        provider_declaration_qualified=True,
        classifier_metadata={},
        present_fields=declaration.normalized_fields,
    )
    return FinancialPeriodCandidate.create(
        artifact=artifact,
        period_identity=FinancialPeriodIdentity.create(
            instrument_identity=identity,
            capability=FinancialCapability.RATIO_FAMILY,
            statement_type=None,
            ratio_family=family,
            period_end=period,
            frequency=FinancialReportingFrequency.QUARTERLY,
            company_type=FinancialCompanyType.INDUSTRIAL_NON_BANK,
            consolidation_scope=FinancialConsolidationScope.CONSOLIDATED,
            currency=currency or ("XXX" if provider_id == "tushare" else "CNY"),
            provider_revision_binding=artifact.artifact_identity,
        ),
        filing_metadata=FinancialFilingMetadata(
            ann_date=ann_date,
            f_ann_date=f_ann_date,
            report_type="1",
            comp_type="1",
            update_flag="0",
            retrieved_at=retrieved_at,
            observed_at=observed_at,
            local_provider_revision_identity=artifact.artifact_identity,
            provider_filing_revision_id=provider_revision_id,
            provider_filing_revision_sequence=provider_revision_sequence,
            provider_filing_revision_sequence_qualification=(
                "financial-provider-revision-sequence-qualified-v1"
                if provider_revision_sequence_qualified
                else None
            ),
        ),
        company_type_resolution=resolution,
        fields=fields,
    )
def test_same_provider_revisions_use_exact_operational_order_under_shuffled_input(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    old = datetime(2026, 7, 1, 8, 0, tzinfo=timezone.utc)
    observed = datetime(2026, 7, 2, 8, 0, tzinfo=timezone.utc)
    retrieved = datetime(2026, 7, 2, 9, 0, tzinfo=timezone.utc)
    revisions = (
        _revision(
            value="1",
            ann_date=date(2026, 6, 30),
            observed_at=old,
            retrieved_at=old,
        ),
        _revision(
            value="2",
            ann_date=date(2026, 7, 1),
            observed_at=old,
            retrieved_at=old,
        ),
        _revision(
            value="3",
            ann_date=date(2026, 7, 1),
            observed_at=observed,
            retrieved_at=old,
        ),
        _revision(
            value="4",
            ann_date=date(2026, 7, 1),
            observed_at=observed,
            retrieved_at=retrieved,
            update_flag="0",
        ),
        _revision(
            value="5",
            ann_date=date(2026, 7, 1),
            observed_at=observed,
            retrieved_at=retrieved,
            update_flag="1",
        ),
    )
    tied = revisions[-2:]
    expected = min(
        tied,
        key=lambda candidate: candidate.filing_metadata.local_provider_revision_identity,
    )

    outputs = []
    manifests = []
    for order in (
        revisions,
        tuple(reversed(revisions)),
        (*revisions[2:], *revisions[:2]),
    ):
        result = _route(monkeypatch, tuple(order))
        revision_selection = result.manifest.revision_selections[0]
        outputs.append(revision_selection.model_dump(mode="json"))
        manifests.append(result.manifest.model_dump(mode="json"))
        assert revision_selection.selected_candidate_identity == (
            expected.candidate_identity
        )
        assert revision_selection.ordering_basis == (
            "ann_date_observed_at_retrieved_at_local_revision_identity"
        )
        assert revision_selection.chronology_claim == "operational_only"
        assert revision_selection.conflicted is True

    assert outputs[0] == outputs[1] == outputs[2]
    assert manifests[0] == manifests[1] == manifests[2]


def test_qualified_provider_revision_sequence_supersedes_local_observation_order(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    older_observation = _revision(
        value="7",
        ann_date=date(2026, 6, 30),
        observed_at=datetime(2026, 7, 1, 8, 0, tzinfo=timezone.utc),
        retrieved_at=datetime(2026, 7, 1, 8, 0, tzinfo=timezone.utc),
        provider_revision_id="filing-r2",
        provider_revision_sequence=2,
        provider_revision_sequence_qualified=True,
    )
    locally_newer = _revision(
        value="8",
        ann_date=date(2026, 7, 2),
        observed_at=datetime(2026, 7, 3, 8, 0, tzinfo=timezone.utc),
        retrieved_at=datetime(2026, 7, 3, 8, 0, tzinfo=timezone.utc),
        provider_revision_id="filing-r1",
        provider_revision_sequence=1,
        provider_revision_sequence_qualified=True,
    )

    result = _route(monkeypatch, (locally_newer, older_observation))

    selection = result.manifest.revision_selections[0]
    assert selection.selected_candidate_identity == older_observation.candidate_identity
    assert selection.ordering_basis == "qualified_provider_revision_sequence"
    assert selection.chronology_claim == "qualified_provider_sequence"
    assert selection.conflicted is False
    assert {
        item.candidate_identity: item.disposition
        for item in selection.candidate_dispositions
    } == {
        older_observation.candidate_identity: "selected",
        locally_newer.candidate_identity: "superseded",
    }
    assert result.manifest.conflicts == ()


def test_unqualified_provider_sequence_metadata_cannot_create_chronology_claim(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    sequence_two = _revision(
        value="1",
        observed_at=datetime(2026, 7, 1, 8, 0, tzinfo=timezone.utc),
        retrieved_at=datetime(2026, 7, 1, 8, 0, tzinfo=timezone.utc),
        provider_revision_id="unqualified-revision-2",
        provider_revision_sequence=2,
    )
    locally_later = _revision(
        value="2",
        observed_at=datetime(2026, 7, 2, 8, 0, tzinfo=timezone.utc),
        retrieved_at=datetime(2026, 7, 2, 8, 0, tzinfo=timezone.utc),
        provider_revision_id="unqualified-revision-1",
        provider_revision_sequence=1,
    )

    selection = _route(monkeypatch, (sequence_two, locally_later)).manifest.revision_selections[
        0
    ]

    assert selection.ordering_basis == (
        "ann_date_observed_at_retrieved_at_local_revision_identity"
    )
    assert selection.chronology_claim == "operational_only"
    assert selection.selected_candidate_identity == locally_later.candidate_identity
    assert selection.conflicted is True


def test_duplicate_provider_revision_sequence_fails_closed_to_conflict(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    first = _revision(
        value="1",
        observed_at=datetime(2026, 7, 1, 8, 0, tzinfo=timezone.utc),
        retrieved_at=datetime(2026, 7, 1, 8, 0, tzinfo=timezone.utc),
        provider_revision_id="provider-revision-a",
        provider_revision_sequence=2,
        provider_revision_sequence_qualified=True,
    )
    later_observation = _revision(
        value="2",
        observed_at=datetime(2026, 7, 2, 8, 0, tzinfo=timezone.utc),
        retrieved_at=datetime(2026, 7, 2, 8, 0, tzinfo=timezone.utc),
        provider_revision_id="provider-revision-b",
        provider_revision_sequence=2,
        provider_revision_sequence_qualified=True,
    )

    selection = _route(monkeypatch, (first, later_observation)).manifest.revision_selections[
        0
    ]

    assert selection.ordering_basis == (
        "ann_date_observed_at_retrieved_at_local_revision_identity"
    )
    assert selection.chronology_claim == "operational_only"
    assert selection.selected_candidate_identity == later_observation.candidate_identity
    assert selection.conflicted is True
    assert {
        item.candidate_identity: item.disposition
        for item in selection.candidate_dispositions
    } == {
        first.candidate_identity: "conflicted_unselected",
        later_observation.candidate_identity: "operational_winner_conflicted",
    }


def test_manifest_rejects_rehashed_revision_selection_with_contradictory_lineage(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    winner = _revision(
        value="1",
        provider_revision_id="provider-revision-2",
        provider_revision_sequence=2,
        provider_revision_sequence_qualified=True,
    )
    superseded = _revision(
        value="2",
        provider_revision_id="provider-revision-1",
        provider_revision_sequence=1,
        provider_revision_sequence_qualified=True,
    )
    manifest = _route(monkeypatch, (winner, superseded)).manifest
    revision_selection = manifest.revision_selections[0]
    payload = revision_selection.model_dump(mode="json")
    dispositions = payload["candidate_dispositions"]
    assert isinstance(dispositions, list)
    for disposition in dispositions:
        if disposition["candidate_identity"] == superseded.candidate_identity:
            disposition["artifact_identity"] = winner.artifact.artifact_identity
            disposition["local_provider_revision_identity"] = (
                winner.artifact.artifact_identity
            )
    identity_payload = {
        key: value for key, value in payload.items() if key != "selection_identity"
    }
    encoded = json.dumps(
        identity_payload,
        allow_nan=False,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
    payload["selection_identity"] = (
        "financial-revision-selection:v1:" + sha256(encoded).hexdigest()
    )
    tampered_selection = FinancialRevisionSelection.model_validate(payload)

    with pytest.raises(ValueError, match="revision selection provenance"):
        FinancialAcquisitionManifest.create(
            capability_routing_plan_signature=(
                manifest.capability_routing_plan_signature
            ),
            instrument_identity=manifest.instrument_identity,
            requested_capability=manifest.requested_capability,
            requested_statement_type=manifest.requested_statement_type,
            requested_ratio_family=manifest.requested_ratio_family,
            artifacts=manifest.artifacts,
            acquisition_outcomes=manifest.acquisition_outcomes,
            provider_candidates=manifest.provider_candidates,
            selections=manifest.selections,
            rejected_periods=manifest.rejected_periods,
            overlaps=manifest.overlaps,
            conflicts=manifest.conflicts,
            aggregate_completeness=manifest.aggregate_completeness,
            revision_selections=(tampered_selection,),
        )


def test_identical_payload_bytes_retain_distinct_revision_observation_identities(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    first_observation = _revision(
        value="1",
        observed_at=datetime(2026, 7, 1, 8, 0, tzinfo=timezone.utc),
        retrieved_at=datetime(2026, 7, 1, 8, 0, tzinfo=timezone.utc),
    )
    later_observation = _revision(
        value="1",
        observed_at=datetime(2026, 7, 2, 8, 0, tzinfo=timezone.utc),
        retrieved_at=datetime(2026, 7, 2, 8, 0, tzinfo=timezone.utc),
    )

    result = _route(monkeypatch, (first_observation, later_observation))

    assert first_observation.artifact.payload_sha256 == (
        later_observation.artifact.payload_sha256
    )
    assert first_observation.artifact.artifact_identity != (
        later_observation.artifact.artifact_identity
    )
    assert {
        item.artifact.artifact_identity for item in result.manifest.artifacts
    } == {
        first_observation.artifact.artifact_identity,
        later_observation.artifact.artifact_identity,
    }
    selection = result.manifest.revision_selections[0]
    assert selection.selected_candidate_identity == later_observation.candidate_identity
    assert {
        item.candidate_identity: item.disposition
        for item in selection.candidate_dispositions
    } == {
        first_observation.candidate_identity: "equivalent_unselected",
        later_observation.candidate_identity: "selected",
    }


def test_update_flag_alone_creates_no_ordering_or_supersession_claim(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    flag_zero = _revision(value="1", update_flag="0")
    flag_one = _revision(value="1", update_flag="1")
    expected = min(
        (flag_zero, flag_one),
        key=lambda candidate: candidate.filing_metadata.local_provider_revision_identity,
    )

    forward_manifest = _route(monkeypatch, (flag_zero, flag_one)).manifest
    reverse_manifest = _route(monkeypatch, (flag_one, flag_zero)).manifest
    forward = forward_manifest.revision_selections[0]
    reverse = reverse_manifest.revision_selections[0]

    assert forward == reverse
    assert forward_manifest.model_dump(mode="json") == reverse_manifest.model_dump(
        mode="json"
    )
    assert forward.selected_candidate_identity == expected.candidate_identity
    assert forward.ordering_basis == (
        "ann_date_observed_at_retrieved_at_local_revision_identity"
    )
    assert forward.chronology_claim == "operational_only"
    assert "superseded" not in {
        item.disposition for item in forward.candidate_dispositions
    }


def test_incompatible_selection_key_dimensions_are_rejected_before_comparison(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    selected = _revision(value="1")
    incompatible = (
        _revision(value="1", company_type=FinancialCompanyType.BANK),
        _revision(
            value="1",
            frequency=FinancialReportingFrequency.ANNUAL,
        ),
        _revision(
            value="1",
            consolidation_scope=FinancialConsolidationScope.PARENT,
        ),
        _revision(value="1", currency="USD"),
        _revision(value="1", normalized_unit="USD_BASE_UNIT"),
    )
    candidates = (selected, *incompatible)
    response = FinancialProviderPeriodResponse.available(
        provider_id="tushare",
        response_artifact=selected.artifact,
        retained_artifacts=tuple(
            {
                candidate.artifact.artifact_identity: candidate.artifact
                for candidate in candidates
            }.values()
        ),
        candidates=tuple(reversed(candidates)),
        subrequest_keys=("ticket-05-incompatible-keys",),
        physical_attempt_ids=("ticket-05-incompatible-keys-attempt",),
    )
    result = MainlandFinancialCapabilityRouter(
        routing_plan=_shadow_plan(monkeypatch),
        statement_sources={"tushare": lambda _request: response},
    ).route_statement(
        FinancialStatementRoutingRequest(
            contract_version="financial-statement-routing-request-v2",
            eligibility_policy_version=_POLICY_VERSION,
            instrument_identity=_identity(),
            statement_type=FinancialStatementType.BALANCE_SHEET,
            as_of_date=_AS_OF,
            company_type=FinancialCompanyType.INDUSTRIAL_NON_BANK,
            consolidation_scope=FinancialConsolidationScope.CONSOLIDATED,
            currency="CNY",
            eligible_annual_period_ends=(),
            eligible_reporting_period_ends=(_PERIOD,),
        )
    )

    revision_selection = result.manifest.revision_selections[0]
    assert revision_selection.selected_candidate_identity == selected.candidate_identity
    assert {
        item.candidate_identity for item in revision_selection.candidate_dispositions
    } == {selected.candidate_identity}
    assert {item.candidate_identity for item in result.manifest.rejected_periods} == {
        item.candidate_identity for item in incompatible
    }
    assert result.manifest.overlaps == ()
    assert result.manifest.conflicts == ()
    assert result.conflicted_reporting_period_ends == ()


def test_strict_pit_excludes_later_observed_revision_from_qualified_sequence(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    replay_period = date(2026, 3, 31)
    eligible = _indicator_revision(
        period=replay_period,
        value="1",
        observed_at=datetime(2026, 7, 31, 12, 0, tzinfo=timezone.utc),
        retrieved_at=datetime(2026, 7, 31, 12, 0, tzinfo=timezone.utc),
        provider_revision_id="filing-r1",
        provider_revision_sequence=1,
        provider_revision_sequence_qualified=True,
    )
    later_correction = _indicator_revision(
        period=replay_period,
        value="2",
        observed_at=datetime(2026, 8, 2, 12, 0, tzinfo=timezone.utc),
        retrieved_at=datetime(2026, 8, 2, 12, 0, tzinfo=timezone.utc),
        provider_revision_id="filing-r2",
        provider_revision_sequence=2,
        provider_revision_sequence_qualified=True,
    )
    fillers = tuple(
        _indicator_revision(period=period, value="1")
        for period in _REPORTING_PERIODS
        if period != replay_period
    ) + tuple(
        _indicator_revision(period=date(year, 12, 31), value="1")
        for year in (2021, 2022, 2023)
    )
    candidates = (later_correction, eligible, *fillers)
    response = FinancialProviderPeriodResponse.available(
        provider_id="tushare",
        response_artifact=later_correction.artifact,
        retained_artifacts=tuple(candidate.artifact for candidate in candidates),
        candidates=candidates,
        subrequest_keys=("ticket-05-strict-replay",),
        physical_attempt_ids=("ticket-05-strict-replay-attempt",),
    )
    result = MainlandFinancialCapabilityRouter(
        routing_plan=_shadow_plan(monkeypatch),
        statement_sources={},
        indicator_sources={"tushare": lambda _request: response},
    ).route_indicators(
        FinancialIndicatorRoutingRequest(
            contract_version="financial-indicator-routing-request-v3",
            eligibility_policy_version=_POLICY_VERSION,
            requested_analysis_scope=FinancialAvailabilityScope.STRICT_PIT_ELIGIBLE,
            instrument_identity=_identity(),
            as_of_date=_AS_OF,
            company_type=FinancialCompanyType.INDUSTRIAL_NON_BANK,
            consolidation_scope=FinancialConsolidationScope.CONSOLIDATED,
            currency="CNY",
            ratio_families=(FinancialRatioFamily.PROFIT,),
            eligible_reporting_period_ends=_REPORTING_PERIODS,
        )
    )

    manifest = result.family_manifests[0]
    replay_selection = next(
        item for item in manifest.revision_selections if item.period_end == replay_period
    )
    assert replay_selection.selected_candidate_identity == eligible.candidate_identity
    assert replay_selection.ordering_basis == "qualified_provider_revision_sequence"
    assert later_correction.candidate_identity in {
        rejection.candidate_identity for rejection in manifest.rejected_periods
    }
    assert later_correction.candidate_identity not in {
        item.candidate_identity
        for item in replay_selection.candidate_dispositions
    }


def test_non_critical_difference_is_unselected_without_field_merge_or_route_override(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    gap_period = date(2025, 12, 31)
    conflict_period = date(2025, 9, 30)
    route_winner = _revision(
        value="1",
        observed_at=datetime(2026, 7, 1, 8, 0, tzinfo=timezone.utc),
        retrieved_at=datetime(2026, 7, 1, 8, 0, tzinfo=timezone.utc),
    )
    later_difference = _revision(
        provider_id="akshare_sina",
        value="1",
        observed_at=datetime(2026, 7, 3, 8, 0, tzinfo=timezone.utc),
        retrieved_at=datetime(2026, 7, 3, 8, 0, tzinfo=timezone.utc),
        field_overrides={"inventory": "2"},
    )
    gap = _revision(
        provider_id="akshare_sina",
        period=gap_period,
        value="3",
        observed_at=datetime(2026, 7, 3, 8, 0, tzinfo=timezone.utc),
        retrieved_at=datetime(2026, 7, 3, 8, 0, tzinfo=timezone.utc),
    )
    critical_first = _revision(
        provider_id="akshare_sina",
        period=conflict_period,
        value="4",
    )
    critical_second = _revision(
        provider_id="akshare_sina",
        period=conflict_period,
        value="4",
        field_overrides={"total_assets": "5"},
    )
    responses = {
        "tushare": FinancialProviderPeriodResponse.available(
            provider_id="tushare",
            response_artifact=route_winner.artifact,
            retained_artifacts=(route_winner.artifact,),
            candidates=(route_winner,),
            subrequest_keys=("ticket-05-route-first",),
            physical_attempt_ids=("ticket-05-route-first-attempt",),
        ),
        "akshare_sina": FinancialProviderPeriodResponse.available(
            provider_id="akshare_sina",
            response_artifact=gap.artifact,
            retained_artifacts=(
                later_difference.artifact,
                critical_second.artifact,
                gap.artifact,
                critical_first.artifact,
            ),
            candidates=(critical_second, gap, later_difference, critical_first),
            subrequest_keys=("ticket-05-route-gap",),
            physical_attempt_ids=("ticket-05-route-gap-attempt",),
        ),
    }
    result = MainlandFinancialCapabilityRouter(
        routing_plan=_shadow_plan(monkeypatch),
        statement_sources={
            provider: (lambda _request, response=response: response)
            for provider, response in responses.items()
        },
    ).route_statement(
        FinancialStatementRoutingRequest(
            contract_version="financial-statement-routing-request-v2",
            eligibility_policy_version=_POLICY_VERSION,
            instrument_identity=_identity(),
            statement_type=FinancialStatementType.BALANCE_SHEET,
            as_of_date=_AS_OF,
            company_type=FinancialCompanyType.INDUSTRIAL_NON_BANK,
            consolidation_scope=FinancialConsolidationScope.CONSOLIDATED,
            currency="CNY",
            eligible_annual_period_ends=(),
            eligible_reporting_period_ends=(
                _PERIOD,
                gap_period,
                conflict_period,
            ),
        )
    )

    overlap_selection = next(
        item for item in result.manifest.revision_selections if item.period_end == _PERIOD
    )
    assert overlap_selection.selected_candidate_identity == route_winner.candidate_identity
    assert overlap_selection.selected_provider_route_rank == 0
    assert overlap_selection.conflicted is False
    assert {
        item.candidate_identity: item.disposition
        for item in overlap_selection.candidate_dispositions
    }[later_difference.candidate_identity] == "non_critical_difference_unselected"
    assert result.manifest.overlaps[0].disposition == "non_critical_difference"
    selected = next(
        item for item in result.selected_candidates if item.period_identity.period_end == _PERIOD
    )
    assert {
        field.normalized_field: field.normalized_value for field in selected.fields
    }["inventory"] == "1"
    assert result.conflicted_reporting_period_ends == (conflict_period,)
    assert len(result.manifest.conflicts) == 1
    assert {
        candidate.period_identity.period_end
        for candidate in result.selected_candidates
    } == {_PERIOD, gap_period, conflict_period}
    assert tuple(call.provider_id for call in result.provider_calls) == (
        "tushare",
        "akshare_sina",
    )
    assert result.physical_attempt_ids == (
        "ticket-05-route-first-attempt",
        "ticket-05-route-gap-attempt",
    )


def test_conflict_state_round_trips_and_bars_only_affected_period_source_facts(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    asset = resolve_run_asset_configuration(
        "600895.SS",
        config=copy.deepcopy(DEFAULT_CONFIG),
    )
    first = _revision(
        value="1",
        instrument_identity=asset.instrument_identity,
    )
    second = _revision(
        value="2",
        update_flag="1",
        instrument_identity=asset.instrument_identity,
    )
    sibling_period = date(2025, 12, 31)
    sibling = _revision(
        period=sibling_period,
        value="3",
        instrument_identity=asset.instrument_identity,
    )
    expected_winner = min(
        (first, second),
        key=lambda candidate: candidate.filing_metadata.local_provider_revision_identity,
    )
    event = _attempt_event(ordinal=5)
    response = FinancialProviderPeriodResponse.available(
        provider_id="tushare",
        response_artifact=first.artifact,
        retained_artifacts=(second.artifact, sibling.artifact, first.artifact),
        candidates=(second, sibling, first),
        subrequest_keys=(event.request_key,),
        physical_attempt_ids=(event.attempt_event_id,),
        physical_attempt_events=(event,),
    )
    plan = _shadow_plan(monkeypatch)
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
    selection_request = FinancialStatementRoutingRequest(
        contract_version="financial-statement-routing-request-v2",
        eligibility_policy_version=_POLICY_VERSION,
        instrument_identity=asset.instrument_identity,
        statement_type=FinancialStatementType.BALANCE_SHEET,
        as_of_date=_AS_OF,
        company_type=FinancialCompanyType.INDUSTRIAL_NON_BANK,
        consolidation_scope=FinancialConsolidationScope.CONSOLIDATED,
        currency="CNY",
        eligible_annual_period_ends=(),
        eligible_reporting_period_ends=(_PERIOD, sibling_period),
    )
    tool_request = FinancialToolRequest(
        tool_name="get_balance_sheet",
        statement_type=FinancialStatementType.BALANCE_SHEET,
        frequency=FinancialReportingFrequency.QUARTERLY,
        as_of_date=_AS_OF,
        tool_call_id="ticket-05-conflict-first",
    )
    dispatcher = FinancialToolDispatcher(
        instrument_identity=asset.instrument_identity,
        run_asset_configuration=asset,
        provider_chains={"get_balance_sheet": provider_chain},
        qualified_statement_router=MainlandFinancialCapabilityRouter(
            routing_plan=plan,
            statement_sources={"tushare": lambda _request: response},
        ),
        run_scope_id="ticket-05-conflict-round-trip",
    )
    dispatch = dispatcher.dispatch_statement_selection(
        tool_request,
        selection_request,
    )

    manifest = dispatch.routing_result.manifest
    conflict_selection = next(
        item for item in manifest.revision_selections if item.period_end == _PERIOD
    )
    assert conflict_selection.selected_candidate_identity == (
        expected_winner.candidate_identity
    )
    assert conflict_selection.conflicted is True
    assert dispatch.routing_result.conflicted_reporting_period_ends == (_PERIOD,)
    assert {
        candidate.period_identity.period_end
        for candidate in dispatch.routing_result.selected_candidates
    } == {_PERIOD, sibling_period}

    evidence = project_financial_selection_evidence(
        EvidenceState(instrument_identity=asset.instrument_identity),
        dispatch,
    )
    assert {fact.effective_date for fact in evidence.source_facts} == {
        sibling_period.isoformat()
    }
    assert next(
        item
        for item in evidence.financial_revision_selections
        if item.period_end == _PERIOD
    ).selection_identity == conflict_selection.selection_identity
    assert {
        observation.candidate_identity: observation.revision_disposition
        for observation in evidence.financial_revision_observations
    } == {
        expected_winner.candidate_identity: "operational_winner_conflicted",
        (
            second.candidate_identity
            if expected_winner is first
            else first.candidate_identity
        ): "conflicted_unselected",
        sibling.candidate_identity: "selected",
    }

    checkpoint = dispatcher.checkpoint_ledger()
    checkpoint_manifest = checkpoint["qualified_statement_selections"][0][
        "terminal"
    ]["routing_result"]["manifest"]
    assert checkpoint_manifest["revision_selections"] == [
        item.model_dump(mode="json") for item in manifest.revision_selections
    ]
    restored = FinancialToolDispatcher(
        instrument_identity=asset.instrument_identity,
        run_asset_configuration=asset,
        provider_chains={"get_balance_sheet": provider_chain},
        qualified_statement_router=MainlandFinancialCapabilityRouter(
            routing_plan=plan,
            statement_sources={
                "tushare": lambda _request: pytest.fail(
                    "checkpoint restore repeated provider I/O"
                )
            },
        ),
        run_scope_id="ticket-05-conflict-round-trip",
        checkpoint_ledger=checkpoint,
    )
    replay = restored.dispatch_statement_selection(
        tool_request.model_copy(update={"tool_call_id": "ticket-05-conflict-replay"}),
        selection_request,
    )
    assert replay.routing_result.manifest.model_dump(mode="json") == (
        manifest.model_dump(mode="json")
    )

    audit = project_financial_dispatch_ledger(
        checkpoint,
        run_asset_configuration=asset,
        capability_routing_plan=plan,
        run_scope_id="ticket-05-conflict-round-trip",
    )
    assert audit is not None
    audit_manifest = audit.manifest_requests[0].manifests[0]
    assert audit_manifest.revision_selections == manifest.revision_selections
    tool_manifest = dispatch.to_tool_message().artifact["manifests"][0]
    assert tool_manifest["revision_selections"] == [
        item.model_dump(mode="json") for item in manifest.revision_selections
    ]
    report = "\n".join(_render_financial_dispatch_operations(audit))
    assert expected_winner.candidate_identity in report
    assert "operational_winner_conflicted" in report
    assert "ann_date_observed_at_retrieved_at_local_revision_identity" in report


def test_equivalent_later_route_is_visible_without_coverage_or_source_fact_inflation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    asset = resolve_run_asset_configuration(
        "600895.SS",
        config=copy.deepcopy(DEFAULT_CONFIG),
    )
    overlap_period = _PERIOD
    gap_period = date(2025, 12, 31)
    selected = _revision(
        value="1",
        instrument_identity=asset.instrument_identity,
    )
    equivalent = _revision(
        provider_id="akshare_sina",
        value="1",
        observed_at=datetime(2026, 7, 3, 8, 0, tzinfo=timezone.utc),
        retrieved_at=datetime(2026, 7, 3, 8, 0, tzinfo=timezone.utc),
        instrument_identity=asset.instrument_identity,
    )
    gap = _revision(
        provider_id="akshare_sina",
        period=gap_period,
        value="2",
        instrument_identity=asset.instrument_identity,
    )
    first_event = _attempt_event(ordinal=6)
    gap_event = _attempt_event(ordinal=7)
    responses = {
        "tushare": FinancialProviderPeriodResponse.available(
            provider_id="tushare",
            response_artifact=selected.artifact,
            retained_artifacts=(selected.artifact,),
            candidates=(selected,),
            subrequest_keys=(first_event.request_key,),
            physical_attempt_ids=(first_event.attempt_event_id,),
            physical_attempt_events=(first_event,),
        ),
        "akshare_sina": FinancialProviderPeriodResponse.available(
            provider_id="akshare_sina",
            response_artifact=gap.artifact,
            retained_artifacts=(equivalent.artifact, gap.artifact),
            candidates=(equivalent, gap),
            subrequest_keys=(gap_event.request_key,),
            physical_attempt_ids=(gap_event.attempt_event_id,),
            physical_attempt_events=(gap_event,),
        ),
    }
    plan = _shadow_plan(monkeypatch)
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
    dispatcher = FinancialToolDispatcher(
        instrument_identity=asset.instrument_identity,
        run_asset_configuration=asset,
        provider_chains={"get_balance_sheet": provider_chain},
        qualified_statement_router=MainlandFinancialCapabilityRouter(
            routing_plan=plan,
            statement_sources={
                provider: (lambda _request, response=response: response)
                for provider, response in responses.items()
            },
        ),
        run_scope_id="ticket-05-equivalent",
    )
    dispatch = dispatcher.dispatch_statement_selection(
        FinancialToolRequest(
            tool_name="get_balance_sheet",
            statement_type=FinancialStatementType.BALANCE_SHEET,
            frequency=FinancialReportingFrequency.QUARTERLY,
            as_of_date=_AS_OF,
            tool_call_id="ticket-05-equivalent",
        ),
        FinancialStatementRoutingRequest(
            contract_version="financial-statement-routing-request-v2",
            eligibility_policy_version=_POLICY_VERSION,
            instrument_identity=asset.instrument_identity,
            statement_type=FinancialStatementType.BALANCE_SHEET,
            as_of_date=_AS_OF,
            company_type=FinancialCompanyType.INDUSTRIAL_NON_BANK,
            consolidation_scope=FinancialConsolidationScope.CONSOLIDATED,
            currency="CNY",
            eligible_annual_period_ends=(),
            eligible_reporting_period_ends=(overlap_period, gap_period),
        ),
    )

    manifest = dispatch.routing_result.manifest
    overlap_selection = next(
        item
        for item in manifest.revision_selections
        if item.period_end == overlap_period
    )
    assert overlap_selection.selected_candidate_identity == selected.candidate_identity
    assert {
        item.candidate_identity: item.disposition
        for item in overlap_selection.candidate_dispositions
    }[equivalent.candidate_identity] == "equivalent_unselected"
    assert len(manifest.selections) == 2
    assert manifest.aggregate_completeness.covered_reporting_period_ends == (
        overlap_period,
        gap_period,
    )
    evidence = project_financial_selection_evidence(
        EvidenceState(instrument_identity=asset.instrument_identity),
        dispatch,
    )
    assert len(evidence.source_facts) == len(selected.fields) + len(gap.fields)
    assert {
        observation.candidate_identity: observation.revision_disposition
        for observation in evidence.financial_revision_observations
    }[equivalent.candidate_identity] == "equivalent_unselected"
    assert tuple(
        call.provider_id for call in dispatch.routing_result.provider_calls
    ) == ("tushare", "akshare_sina")
    assert dispatch.routing_result.physical_attempt_ids == (
        first_event.attempt_event_id,
        gap_event.attempt_event_id,
    )


def test_ratio_overlap_with_incompatible_currency_is_rejected_before_comparison(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    overlap_period = _PERIOD
    gap_period = date(2025, 12, 31)
    tushare_overlap = _indicator_revision(
        period=overlap_period,
        value="1",
    )
    akshare_overlap = _indicator_revision(
        provider_id="akshare",
        period=overlap_period,
        value="2",
    )
    akshare_gap = _indicator_revision(
        provider_id="akshare",
        period=gap_period,
        value="3",
    )
    responses = {
        "tushare": FinancialProviderPeriodResponse.available(
            provider_id="tushare",
            response_artifact=tushare_overlap.artifact,
            retained_artifacts=(tushare_overlap.artifact,),
            candidates=(tushare_overlap,),
            subrequest_keys=("ticket-05-ratio-key-tushare",),
            physical_attempt_ids=("ticket-05-ratio-key-tushare-attempt",),
        ),
        "akshare": FinancialProviderPeriodResponse.available(
            provider_id="akshare",
            response_artifact=akshare_gap.artifact,
            retained_artifacts=(akshare_overlap.artifact, akshare_gap.artifact),
            candidates=(akshare_overlap, akshare_gap),
            subrequest_keys=("ticket-05-ratio-key-akshare",),
            physical_attempt_ids=("ticket-05-ratio-key-akshare-attempt",),
        ),
    }

    result = MainlandFinancialCapabilityRouter(
        routing_plan=_shadow_plan(monkeypatch),
        statement_sources={},
        indicator_sources={
            provider_id: (lambda _request, response=response: response)
            for provider_id, response in responses.items()
        },
    ).route_indicators(
        FinancialIndicatorRoutingRequest(
            contract_version="financial-indicator-routing-request-v3",
            eligibility_policy_version=_POLICY_VERSION,
            requested_analysis_scope=FinancialAvailabilityScope.CURRENT_ONLY,
            instrument_identity=_identity(),
            as_of_date=_AS_OF,
            company_type=FinancialCompanyType.INDUSTRIAL_NON_BANK,
            consolidation_scope=FinancialConsolidationScope.CONSOLIDATED,
            currency="CNY",
            ratio_families=(FinancialRatioFamily.PROFIT,),
            eligible_reporting_period_ends=(overlap_period, gap_period),
        )
    )

    manifest = result.family_manifests[0]
    assert {
        candidate.candidate_identity for candidate in result.selected_candidates
    } == {tushare_overlap.candidate_identity, akshare_gap.candidate_identity}
    assert {
        item.candidate.candidate_identity for item in manifest.provider_candidates
    } == {
        tushare_overlap.candidate_identity,
        akshare_overlap.candidate_identity,
        akshare_gap.candidate_identity,
    }
    rejection = next(
        item
        for item in manifest.rejected_periods
        if item.candidate_identity == akshare_overlap.candidate_identity
    )
    assert rejection.reasons == (
        "incompatible_currency",
    )
    assert manifest.overlaps == ()
    assert manifest.conflicts == ()
    assert manifest.aggregate_completeness.covered_reporting_period_ends == (
        overlap_period,
        gap_period,
    )

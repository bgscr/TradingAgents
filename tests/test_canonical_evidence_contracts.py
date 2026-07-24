"""Contract-level regressions for canonical evidence source boundaries."""

from __future__ import annotations

import warnings
from hashlib import sha256

import pytest
from langchain_core.messages import AIMessage, ToolMessage
from pydantic import ValidationError

from tradingagents.dataflows.acquisition import AcquisitionRequest
from tradingagents.evidence import (
    EVIDENCE_CONTRACT_VERSION,
    AcquisitionUnavailableReason,
    AdmissionGateResult,
    AnalysisDiagnosticCode,
    AnalystEvidenceReport,
    CalculationDefinition,
    CalculationLineage,
    CapabilityProfile,
    ClaimValidation,
    EvidenceCapability,
    EvidenceReadiness,
    EvidenceSource,
    EvidenceState,
    EvidenceStatus,
    IdentityProvenance,
    InstrumentIdentityEvidence,
    InstrumentKind,
    MaterialClaim,
    SourceAcquisitionAvailable,
    SourceAcquisitionUnavailable,
    SourceArtifact,
    SourceFact,
    ToolExecutionEvidenceEnvelope,
    build_tool_evidence_state,
    calculation_readiness_outcome,
    capability_profile_for,
    evaluate_preflight_gate,
    make_source_acquisition_outcome,
    merge_source_facts,
    validate_calculation_lineage,
)


@pytest.mark.unit
@pytest.mark.parametrize(
    ("target", "value", "valid"),
    (
        ("request.capability", "news?token=secret", False),
        ("request.source_ref", "https://feed.example/items", False),
        ("request.tool_name", "get news", False),
        ("available.provider", "Authorization: Bearer secret", False),
        ("available.capability", "news/latest", False),
        ("available.source_ref", "api_key=secret", False),
        ("unavailable.provider", " provider ", False),
        ("unavailable.capability", "news\theader", False),
        ("unavailable.source_ref", "news?token=secret", False),
        ("envelope.tool_name", "get_news?api_key=secret", False),
        ("envelope.capability", "news capability", False),
        ("envelope.source_ref", "Header: value", False),
        ("available.artifact_mismatch", "mismatch", False),
        ("request.source_ref", "acq.v1:news:" + "a" * 64, True),
        ("available.provider", "provider.v1", True),
        ("envelope.capability", "news.latest", True),
    ),
)
def test_acquisition_metadata_is_a_safe_opaque_token_boundary(
    target: str,
    value: str,
    valid: bool,
):
    safe_ref = "acq.v1:test:" + "a" * 64
    artifact_ref = (
        "acq.v1:test:" + "b" * 64
        if target == "available.artifact_mismatch"
        else safe_ref
    )
    artifact_text = "validated payload"
    artifact = SourceArtifact(
        artifact_sha256=sha256(artifact_text.encode()).hexdigest(),
        source_ref=artifact_ref,
        tool_call_id="call-boundary",
        tool_name="get_news",
        raw_text=artifact_text,
    )
    kind, field = target.split(".", 1)
    request_payload = {
        "capability": "news.latest",
        "source_ref": safe_ref,
        "tool_call_id": "call-boundary",
        "tool_name": "get_news",
    }
    available_payload = {
        "outcome": "available",
        "provider": "provider.v1",
        "capability": "news.latest",
        "source_ref": safe_ref,
        "attempt": 1,
        "retrieved_at": "2026-07-20T12:00:00Z",
        "artifact": artifact,
    }
    unavailable_payload = {
        "outcome": "unavailable",
        "provider": "provider.v1",
        "capability": "news.latest",
        "source_ref": safe_ref,
        "attempt": 1,
        "retrieved_at": "2026-07-20T12:00:00Z",
        "retryable": False,
        "reason": AcquisitionUnavailableReason.NO_DATA,
    }
    envelope_payload = {
        "tool_call_id": "call-boundary",
        "tool_name": "get_news",
        "source_ref": safe_ref,
        "capability": "news.latest",
        "acquisition_outcomes": (available_payload,),
        "selected_artifact": artifact,
    }
    model, payload = {
        "request": (AcquisitionRequest, request_payload),
        "available": (SourceAcquisitionAvailable, available_payload),
        "unavailable": (SourceAcquisitionUnavailable, unavailable_payload),
        "envelope": (ToolExecutionEvidenceEnvelope, envelope_payload),
    }[kind]
    if field != "artifact_mismatch":
        payload[field] = value

    if valid:
        model.model_validate(payload)
    else:
        with pytest.raises(ValidationError):
            model.model_validate(payload)


@pytest.mark.unit
@pytest.mark.parametrize(
    "retrieved_at",
    (
        "unknown",
        "2026-07-20T12:00:00",
        "2026-07-20T20:00:00+08:00",
    ),
)
@pytest.mark.parametrize(
    "outcome_type",
    (SourceAcquisitionAvailable, SourceAcquisitionUnavailable),
)
def test_source_acquisition_outcomes_require_concrete_utc_timestamps(
    outcome_type,
    retrieved_at,
):
    source_ref = "acq.v1:test:" + "c" * 64
    common = {
        "provider": "provider.v1",
        "capability": "news.latest",
        "source_ref": source_ref,
        "attempt": 1,
        "retrieved_at": retrieved_at,
    }
    if outcome_type is SourceAcquisitionAvailable:
        artifact_text = "validated payload"
        payload = {
            **common,
            "artifact": SourceArtifact(
                artifact_sha256=sha256(artifact_text.encode()).hexdigest(),
                source_ref=source_ref,
                tool_call_id="call-timestamp",
                tool_name="get_news",
                raw_text=artifact_text,
            ),
        }
    else:
        payload = {
            **common,
            "retryable": False,
            "reason": AcquisitionUnavailableReason.NO_DATA,
        }

    with pytest.raises(ValidationError, match="UTC"):
        outcome_type.model_validate(payload)


@pytest.mark.unit
def test_evidence_source_is_a_versioned_closed_boundary_contract():
    source = EvidenceSource(
        source_id="market_snapshot",
        status=EvidenceStatus.AVAILABLE,
        required=True,
    )

    assert source.contract_version == "1.0"
    with pytest.raises(ValidationError):
        EvidenceSource.model_validate(
            {**source.model_dump(mode="json"), "unregistered_field": "rejected"}
        )
    with pytest.raises(ValidationError):
        EvidenceSource.model_validate(
            {**source.model_dump(mode="json"), "source_id": "   "}
        )


@pytest.mark.unit
@pytest.mark.parametrize(
    ("model", "payload"),
    (
        (
            MaterialClaim,
            {
                "claim_id": "market.latest_close",
                "analyst": "market",
                "statement": "The close was 12.34.",
                "source_refs": ("snapshot:600895.SS:2026-07-18",),
                "source_quote": "The close was 12.34.",
            },
        ),
        (
            ClaimValidation,
            {
                "claim_id": "market.latest_close",
                "status": "supported",
            },
        ),
        (
            AnalystEvidenceReport,
            {"report_markdown": "## Market Analysis\n\nEvidence remained stable."},
        ),
    ),
    ids=("material-claim", "claim-validation", "analyst-evidence-report"),
)
def test_nested_canonical_evidence_models_are_versioned_and_closed(model, payload):
    instance = model.model_validate(payload)

    assert instance.contract_version == "1.0"
    with pytest.raises(ValidationError):
        model.model_validate({**payload, "unknown_nested_field": "rejected"})


@pytest.mark.unit
def test_admission_result_is_versioned_closed_and_has_no_scalar_coverage():
    result = AdmissionGateResult(
        admitted=False,
        readiness=EvidenceReadiness.INSUFFICIENT,
        diagnostics=("required facts unavailable",),
        diagnostic_codes=(
            AnalysisDiagnosticCode.REQUIRED_EVIDENCE_UNAVAILABLE,
        ),
    )

    assert result.model_dump(mode="json") == {
        "contract_version": "1.0",
        "admitted": False,
        "readiness": "insufficient",
        "diagnostics": ["required facts unavailable"],
        "diagnostic_codes": ["required_evidence_unavailable"],
    }
    with pytest.raises(ValidationError):
        AdmissionGateResult.model_validate(
            {**result.model_dump(mode="json"), "coverage": 1.0}
        )


@pytest.mark.unit
def test_source_artifact_digest_must_match_exact_utf8_raw_text():
    raw_text = "收盘价 | 12.34 元"
    digest = sha256(raw_text.encode("utf-8")).hexdigest()

    artifact = SourceArtifact(
        artifact_sha256=digest,
        source_ref="market_snapshot:600895.SS:2026-07-18",
        tool_call_id="market-call-1",
        tool_name="get_market_data",
        raw_text=raw_text,
    )

    assert artifact.artifact_sha256 == digest
    with pytest.raises(ValidationError, match="raw_text"):
        SourceArtifact.model_validate(
            {**artifact.model_dump(mode="json"), "raw_text": "收盘价 | 99.99 元"}
        )


@pytest.mark.unit
def test_source_fact_requires_explicit_canonical_semantics():
    excerpt = SourceFact(
        fact_id="fact:" + "1" * 64,
        source_ref="market_snapshot:600895.SS:2026-07-18",
        tool_call_id="market-call-1",
        tool_name="get_market_data",
        artifact_sha256="a" * 64,
        raw_text="收盘价 12.34 元",
        source_span_start=10,
        source_span_end=21,
    )

    assert excerpt.fact_kind == "excerpt"
    with pytest.raises(ValidationError, match="canonical_field"):
        SourceFact.model_validate(
            {
                **excerpt.model_dump(mode="json"),
                "fact_kind": "canonical",
                "normalized_value": "12.34",
                "unit": "CNY",
                "instrument_symbol": "600895.SS",
                "effective_date": "2026-07-18",
            }
        )


@pytest.mark.unit
def test_source_fact_span_must_describe_its_exact_nonblank_excerpt():
    payload = {
        "fact_id": "fact:" + "1" * 64,
        "source_ref": "market_snapshot:600895.SS:2026-07-18",
        "tool_call_id": "market-call-1",
        "tool_name": "get_market_data",
        "artifact_sha256": "a" * 64,
        "raw_text": "close 12.34",
        "source_span_start": 10,
        "source_span_end": 21,
    }

    assert SourceFact.model_validate(payload).raw_text == "close 12.34"
    with pytest.raises(ValidationError, match="source span"):
        SourceFact.model_validate({**payload, "source_span_end": 22})


@pytest.mark.unit
def test_source_artifact_requires_lowercase_digest_and_nonblank_source_metadata():
    raw_text = "close 12.34"
    payload = {
        "artifact_sha256": sha256(raw_text.encode("utf-8")).hexdigest(),
        "source_ref": "market_snapshot:600895.SS:2026-07-18",
        "tool_call_id": "market-call-1",
        "tool_name": "get_market_data",
        "raw_text": raw_text,
    }

    for field in ("source_ref", "tool_call_id", "tool_name"):
        with pytest.raises(ValidationError):
            SourceArtifact.model_validate({**payload, field: "   "})
    with pytest.raises(ValidationError):
        SourceArtifact.model_validate(
            {**payload, "artifact_sha256": payload["artifact_sha256"].upper()}
        )


@pytest.mark.unit
def test_source_fact_requires_constrained_nonblank_identifiers():
    payload = {
        "fact_id": "fact:" + "1" * 64,
        "source_ref": "market_snapshot:600895.SS:2026-07-18",
        "tool_call_id": "market-call-1",
        "tool_name": "get_market_data",
        "artifact_sha256": "a" * 64,
        "raw_text": "close 12.34",
        "source_span_start": 10,
        "source_span_end": 21,
    }

    for field in ("source_ref", "tool_call_id", "tool_name"):
        with pytest.raises(ValidationError):
            SourceFact.model_validate({**payload, field: "   "})
    with pytest.raises(ValidationError):
        SourceFact.model_validate({**payload, "fact_id": "runtime-id"})
    with pytest.raises(ValidationError):
        SourceFact.model_validate({**payload, "artifact_sha256": "A" * 64})


@pytest.mark.unit
@pytest.mark.parametrize(
    "order",
    ((0, 1), (1, 0)),
    ids=("original-first", "conflict-first"),
)
def test_evidence_state_rejects_conflicting_source_fact_id_redefinitions(order):
    fact_id = "fact:" + "1" * 64
    payload = {
        "fact_id": fact_id,
        "source_ref": "market_snapshot:600895.SS:2026-07-18",
        "tool_call_id": "market-call-1",
        "tool_name": "get_market_data",
        "artifact_sha256": "a" * 64,
        "raw_text": "close 12.34",
        "source_span_start": 10,
        "source_span_end": 21,
    }
    facts = (
        SourceFact.model_validate(payload),
        SourceFact.model_validate({**payload, "raw_text": "close 98.76"}),
    )

    with pytest.raises(
        ValueError,
        match=f"Source fact ID {fact_id!r} was redefined\\.",
    ):
        EvidenceState(source_facts=tuple(facts[index] for index in order))


@pytest.mark.unit
@pytest.mark.parametrize(
    "order",
    ((0, 1), (1, 0)),
    ids=("original-first", "conflict-first"),
)
def test_merge_source_facts_rejects_conflicts_in_a_forged_evidence_state(order):
    fact_id = "fact:" + "1" * 64
    payload = {
        "fact_id": fact_id,
        "source_ref": "market_snapshot:600895.SS:2026-07-18",
        "tool_call_id": "market-call-1",
        "tool_name": "get_market_data",
        "artifact_sha256": "a" * 64,
        "raw_text": "close 12.34",
        "source_span_start": 10,
        "source_span_end": 21,
    }
    facts = (
        SourceFact.model_validate(payload),
        SourceFact.model_validate({**payload, "raw_text": "close 98.76"}),
    )
    valid = EvidenceState(source_facts=(facts[0],))
    forged = valid.model_copy(
        update={"source_facts": tuple(facts[index] for index in order)}
    )

    with pytest.raises(
        ValueError,
        match=f"Source fact ID {fact_id!r} was redefined\\.",
    ):
        merge_source_facts(forged, ())


@pytest.mark.unit
def test_evidence_state_canonicalizes_exact_duplicate_source_facts_in_first_seen_order():
    first = SourceFact(
        fact_id="fact:" + "1" * 64,
        source_ref="market_snapshot:600895.SS:2026-07-18",
        tool_call_id="market-call-1",
        tool_name="get_market_data",
        artifact_sha256="a" * 64,
        raw_text="close 12.34",
        source_span_start=10,
        source_span_end=21,
    )
    second = SourceFact.model_validate(
        {
            **first.model_dump(mode="json"),
            "fact_id": "fact:" + "2" * 64,
            "tool_call_id": "market-call-2",
            "artifact_sha256": "b" * 64,
            "raw_text": "close 98.76",
        }
    )

    evidence = EvidenceState(source_facts=(second, first, second))

    assert evidence.source_facts == (second, first)


@pytest.mark.unit
def test_canonical_contracts_are_versioned_and_closed_without_blanket_strict_mode():
    identity = InstrumentIdentityEvidence(
        symbol="510500.SS",
        venue="XSHG",
        instrument_kind="fund",
        currency="CNY",
        provenance={
            "provider": "mainland-security-master",
            "source_ref": "security-master:510500.SS",
            "retrieved_at": "2026-07-19T12:00:00+00:00",
            "artifact_sha256": "a" * 64,
        },
    )

    assert identity.contract_version == EVIDENCE_CONTRACT_VERSION
    assert identity.instrument_kind is InstrumentKind.FUND
    assert identity.display_name is None
    assert identity.is_authoritative is True
    with pytest.raises(ValidationError):
        InstrumentIdentityEvidence.model_validate(
            {**identity.model_dump(mode="json"), "unregistered_field": "rejected"}
        )


@pytest.mark.unit
def test_legacy_identity_checkpoint_is_readable_but_not_authoritative():
    restored = InstrumentIdentityEvidence.model_validate(
        {"symbol": "NVDA", "name": "NVIDIA"}
    )

    assert restored.display_name == "NVIDIA"
    assert restored.name == "NVIDIA"
    assert restored.is_authoritative is False


@pytest.mark.unit
def test_instrument_kind_selects_fund_capabilities_without_company_assumptions():
    profile = capability_profile_for(InstrumentKind.FUND)

    assert EvidenceCapability.INSTRUMENT_NEWS in profile.optional_capabilities
    assert EvidenceCapability.SOCIAL_SENTIMENT in profile.optional_capabilities
    assert profile.requires(EvidenceCapability.INSTRUMENT_NEWS) is False
    assert EvidenceCapability.NAV_PREMIUM in profile.all_capabilities
    assert EvidenceCapability.TRACKING_ERROR in profile.all_capabilities
    assert EvidenceCapability.COMPANY_FINANCIALS not in profile.all_capabilities
    assert capability_profile_for(InstrumentKind.EQUITY) != profile


@pytest.mark.unit
def test_capability_profile_closes_and_validates_runtime_analyst_applicability():
    equity = capability_profile_for(InstrumentKind.EQUITY)
    fund = capability_profile_for(InstrumentKind.FUND)

    assert EvidenceCapability.INSTRUMENT_NEWS in equity.optional_capabilities
    assert EvidenceCapability.SOCIAL_SENTIMENT in equity.optional_capabilities
    assert equity.requires(EvidenceCapability.MARKET_SNAPSHOT) is True
    assert equity.applicable_analysts == (
        "market",
        "social",
        "news",
        "fundamentals",
    )
    assert fund.applicable_analysts == ("market", "social", "news")
    with pytest.raises(ValidationError, match="unique"):
        CapabilityProfile(
            profile_id="invalid.duplicate.v1",
            instrument_kind=InstrumentKind.EQUITY,
            required_capabilities=(
                EvidenceCapability.MARKET_SNAPSHOT,
                EvidenceCapability.MARKET_SNAPSHOT,
            ),
            applicable_analysts=("market",),
        )
    with pytest.raises(ValidationError, match="disjoint"):
        CapabilityProfile(
            profile_id="invalid.overlap.v1",
            instrument_kind=InstrumentKind.EQUITY,
            required_capabilities=(EvidenceCapability.MARKET_SNAPSHOT,),
            optional_capabilities=(EvidenceCapability.MARKET_SNAPSHOT,),
            applicable_analysts=("market",),
        )
    with pytest.raises(ValidationError, match="unique"):
        CapabilityProfile(
            profile_id="invalid.analysts.v1",
            instrument_kind=InstrumentKind.EQUITY,
            required_capabilities=(EvidenceCapability.MARKET_SNAPSHOT,),
            applicable_analysts=("market", "market"),
        )


@pytest.mark.unit
def test_source_acquisition_outcomes_keep_provider_errors_outside_artifacts():
    unavailable = make_source_acquisition_outcome(
        provider="news-provider",
        capability="news",
        attempt=1,
        retrieved_at="2026-07-19T12:00:00+00:00",
        source_ref="get_news:510500.SS:2026-07-19",
        tool_call_id="runtime-call-1",
        tool_name="get_news",
        content="Too Many Requests: retry after 60 seconds",
        status="error",
    )

    assert isinstance(unavailable, SourceAcquisitionUnavailable)
    assert unavailable.reason is AcquisitionUnavailableReason.RATE_LIMITED
    assert unavailable.retryable is True
    assert not hasattr(unavailable, "artifact")

    content = "NAV premium | 0.42%"
    available = make_source_acquisition_outcome(
        provider="fund-provider",
        capability="nav_premium",
        attempt=1,
        retrieved_at="2026-07-19T12:00:00+00:00",
        source_ref="get_fund_nav:510500.SS:2026-07-19",
        tool_call_id="runtime-call-2",
        tool_name="get_fund_nav",
        content=content,
        status="success",
    )

    assert isinstance(available, SourceAcquisitionAvailable)
    assert available.artifact.raw_text == content
    assert available.artifact.artifact_sha256 == sha256(content.encode()).hexdigest()


@pytest.mark.unit
def test_evidence_state_canonicalizes_acquisition_outcomes_without_warnings():
    primary = SourceAcquisitionUnavailable(
        provider="primary",
        provider_order=0,
        capability="market_snapshot",
        source_ref="acq.v1:market_snapshot:" + "a" * 64,
        attempt=1,
        retrieved_at="2026-07-19T12:00:00Z",
        retryable=True,
        reason=AcquisitionUnavailableReason.PROVIDER_ERROR,
    )
    fallback = SourceAcquisitionUnavailable(
        provider="fallback",
        provider_order=1,
        capability="market_snapshot",
        source_ref=primary.source_ref,
        attempt=1,
        retrieved_at="2026-07-19T12:00:01Z",
        retryable=False,
        reason=AcquisitionUnavailableReason.NO_DATA,
    )

    with warnings.catch_warnings():
        warnings.simplefilter("error")
        direct = EvidenceState(acquisition_outcomes=(fallback, primary))
        restored = EvidenceState.model_validate_json(direct.model_dump_json())

    assert direct.acquisition_outcomes == (primary, fallback)
    assert restored.acquisition_outcomes == direct.acquisition_outcomes
    assert restored.model_dump(mode="json") == direct.model_dump(mode="json")


@pytest.mark.unit
def test_error_tool_text_cannot_materialize_a_source_fact():
    source_ref = "get_news:510500.SS:2026-07-19"
    messages = (
        AIMessage(
            content="",
            tool_calls=[
                {
                    "name": "get_news",
                    "args": {"ticker": "510500.SS"},
                    "id": "runtime-call-1",
                    "type": "tool_call",
                }
            ],
        ),
        ToolMessage(
            content="Too Many Requests",
            tool_call_id="runtime-call-1",
            name="get_news",
            status="error",
        ),
    )
    claim = MaterialClaim(
        claim_id="news.rate_limit",
        analyst="news",
        statement="News supports the thesis.",
        source_refs=(source_ref,),
        source_quote="Too Many Requests",
    )

    evidence = build_tool_evidence_state(
        messages,
        (claim,),
        tool_call_ids_by_source={source_ref: ("runtime-call-1",)},
    )

    assert evidence.source_artifacts == ()
    assert evidence.source_facts == ()
    assert evidence.acquisition_outcomes == ()
    assert evidence.sources[0].status is EvidenceStatus.UNAVAILABLE


@pytest.mark.unit
def test_fact_id_is_invariant_to_llm_statement_and_runtime_tool_call_id():
    source_ref = "get_indicators:NVDA:2026-01-15"
    content = "RSI is 51."

    def materialize(call_id: str, statement: str) -> EvidenceState:
        artifact = SourceArtifact(
            artifact_sha256=sha256(content.encode()).hexdigest(),
            source_ref=source_ref,
            tool_call_id=call_id,
            tool_name="get_indicators",
            raw_text=content,
        )
        envelope = ToolExecutionEvidenceEnvelope(
            tool_call_id=call_id,
            tool_name="get_indicators",
            source_ref=source_ref,
            capability="get_indicators",
            acquisition_outcomes=(
                SourceAcquisitionAvailable(
                    provider="test-adapter",
                    capability="get_indicators",
                    source_ref=source_ref,
                    attempt=1,
                    retrieved_at="2026-01-15T12:00:00Z",
                    artifact=artifact,
                ),
            ),
            selected_artifact=artifact,
        )
        messages = (
            AIMessage(
                content="",
                tool_calls=[
                    {
                        "name": "get_indicators",
                        "args": {"ticker": "NVDA", "indicator": "rsi"},
                        "id": call_id,
                        "type": "tool_call",
                    }
                ],
            ),
            ToolMessage(
                content=content,
                tool_call_id=call_id,
                name="get_indicators",
                artifact=envelope.model_dump(mode="json"),
            ),
        )
        claim = MaterialClaim(
            claim_id="market.rsi",
            analyst="market",
            statement=statement,
            source_refs=(source_ref,),
            source_quote=content,
        )
        return build_tool_evidence_state(
            messages,
            (claim,),
            tool_call_ids_by_source={source_ref: (call_id,)},
        )

    first = materialize("runtime-call-a", "RSI is neutral at 51.")
    second = materialize("runtime-call-b", "The observed RSI value is 51.")

    assert first.source_facts[0].fact_id == second.source_facts[0].fact_id
    assert first.source_facts[0].tool_call_id != second.source_facts[0].tool_call_id


@pytest.mark.unit
def test_calculation_history_is_fail_closed_and_lineage_is_recomputed():
    definition = CalculationDefinition(
        calculation_id="sma.close.200",
        version="1.0.0",
        input_fields=("close",),
        input_frequency="1d",
        minimum_history_rows=200,
        warmup_rows=199,
        adjustment_basis="qfq",
        missing_value_policy="fail",
        formula="mean(last_200(close))",
        implementation_version="tradingagents.indicators@1",
        output_field="close_200_sma",
        output_unit="CNY",
        precision=6,
    )

    unavailable = calculation_readiness_outcome(
        definition,
        observations_available=129,
        adjustment_basis="qfq",
        input_artifact_sha256="a" * 64,
        provider="authoritative-market-snapshot",
        attempt=1,
        retrieved_at="2026-07-19T12:00:00+00:00",
    )
    assert isinstance(unavailable, SourceAcquisitionUnavailable)
    assert unavailable.reason is AcquisitionUnavailableReason.INSUFFICIENT_HISTORY
    assert unavailable.calculation_readiness is not None
    assert unavailable.calculation_readiness.required_observations == 200
    assert unavailable.calculation_readiness.available_observations == 129

    lineage = CalculationLineage(
        calculation_id="sma.close.200",
        calculation_version="1.0.0",
        input_artifact_sha256="a" * 64,
        input_snapshot_id="snapshot:authoritative",
        effective_range_start="2025-10-01",
        effective_range_end="2026-07-19",
        observations_used=200,
        adjustment_basis="qfq",
        implementation_version="tradingagents.indicators@1",
        result_digest="b" * 64,
    )
    validate_calculation_lineage(definition, lineage)

    with pytest.raises(ValueError, match="adjustment basis"):
        validate_calculation_lineage(
            definition,
            lineage.model_copy(update={"adjustment_basis": "unadjusted"}),
        )


@pytest.mark.unit
def test_preflight_checks_only_authoritative_baseline_evidence():
    evidence = EvidenceState(
        instrument_identity=InstrumentIdentityEvidence(
            symbol="510500.SS",
            venue="XSHG",
            instrument_kind="fund",
            currency="CNY",
            provenance=IdentityProvenance(
                    provider="mainland-security-master",
                    source_ref="security-master:510500.SS",
                    retrieved_at="2026-07-19T12:00:00+00:00",
                    artifact_sha256="a" * 64,
            ),
        ),
        market_snapshot={
            "symbol": "510500.SS",
            "provider": "baostock",
            "retrieved_at": "2026-07-19T12:00:00+00:00",
            "adjustment_basis": "qfq",
            "requested_date": "2026-07-19",
            "effective_trading_date": "2026-07-18",
            "history_rows": 129,
            "frame_sha256": "a" * 64,
            "snapshot_id": "snapshot:authoritative",
        },
    )

    result = evaluate_preflight_gate(evidence, minimum_history_rows=200)

    assert result.passed is False
    assert result.readiness is EvidenceReadiness.INSUFFICIENT
    assert result.blockers == (
        "authoritative market snapshot has 129 rows; at least 200 are required",
    )
    assert result.diagnostic_codes == (
        AnalysisDiagnosticCode.HISTORY_INSUFFICIENT,
    )
    assert result.model_dump(mode="json")["passed"] is False


@pytest.mark.unit
def test_preflight_fails_closed_when_identity_has_no_registered_profile():
    evidence = EvidenceState(
        instrument_identity=InstrumentIdentityEvidence(
            symbol="000001.SH",
            venue="XSHG",
            instrument_kind=InstrumentKind.INDEX,
            currency="CNY",
            provenance=IdentityProvenance(
                provider="security-master",
                source_ref="security-master:000001.SH",
                retrieved_at="2026-07-19T12:00:00+00:00",
                artifact_sha256="a" * 64,
            ),
        ),
        market_snapshot={
            "symbol": "000001.SH",
            "provider": "baostock",
            "retrieved_at": "2026-07-19T12:00:00+00:00",
            "adjustment_basis": "qfq",
            "requested_date": "2026-07-19",
            "effective_trading_date": "2026-07-18",
            "history_rows": 129,
            "frame_sha256": "a" * 64,
            "snapshot_id": "snapshot:authoritative",
        },
    )

    result = evaluate_preflight_gate(evidence)

    assert result.passed is False
    assert result.blockers == (
        "no capability profile is registered for instrument kind 'index'",
    )
    assert result.diagnostic_codes == (
        AnalysisDiagnosticCode.DECISION_CONFIGURATION_INVALID,
    )

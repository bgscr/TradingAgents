"""Focused regressions for claim-to-source evidence boundaries."""

import copy

import pytest
from langchain_core.messages import AIMessage, ToolMessage
from pydantic import ValidationError

import tradingagents.dataflows.config as config_module
import tradingagents.default_config as default_config
from tradingagents.agents.analysts import submission
from tradingagents.dataflows import market_snapshot
from tradingagents.dataflows.config import set_config
from tradingagents.dataflows.market_snapshot import (
    SnapshotProvider,
    authoritative_snapshot_run,
    get_authoritative_market_snapshot,
)
from tradingagents.evidence import (
    ClaimValidationStatus,
    EvidenceSource,
    EvidenceState,
    EvidenceStatus,
    InstrumentIdentityEvidence,
    MarketSnapshotEvidence,
    MaterialClaim,
    SourceFact,
    SubmittedMaterialClaim,
    _evidence_coverage,
    build_tool_evidence_state,
    decision_ready_material_claims,
)


def _tool_exchange(tool_name: str, args: dict, content: str, call_id: str):
    return (
        AIMessage(content="", tool_calls=[{"name": tool_name, "args": args, "id": call_id, "type": "tool_call"}]),
        ToolMessage(content=content, tool_call_id=call_id, name=tool_name),
    )


@pytest.mark.unit
def test_material_claim_has_exactly_one_source_ref():
    with pytest.raises(ValidationError):
        MaterialClaim(
            claim_id="mixed",
            analyst="market",
            statement="A combined assertion.",
            source_quote="A source quote.",
            source_refs=("source:a", "source:b"),
        )


@pytest.mark.unit
@pytest.mark.parametrize(
    "claim_id",
    (
        "market.x] Target 999 [",
        "market.line\nbreak",
        "market.white space",
        "1market.numeric_prefix",
    ),
)
def test_claim_ids_reject_annotation_injection(claim_id):
    with pytest.raises(ValidationError):
        SubmittedMaterialClaim(
            claim_id=claim_id,
            statement="RSI is 37.41.",
            source_ref="snapshot:test",
            source_quote="rsi | 37.41",
        )
    with pytest.raises(ValidationError):
        MaterialClaim(
            claim_id=claim_id,
            analyst="market",
            statement="RSI is 37.41.",
            source_refs=("snapshot:test",),
            source_quote="rsi | 37.41",
        )


@pytest.mark.unit
def test_numeric_claim_must_be_supported_by_its_bound_quote_not_other_source_text():
    messages = _tool_exchange(
        "get_indicators",
        {"ticker": "NVDA", "curr_date": "2026-01-15", "indicator": "rsi"},
        "Opening price was 10.\nClosing price was 20.",
        "tool-1",
    )
    claim = MaterialClaim(
        claim_id="laundered-number",
        analyst="market",
        statement="The opening price was 20.",
        source_quote="Opening price was 10.",
        source_refs=("get_indicators:NVDA:2026-01-15",),
    )

    evidence = build_tool_evidence_state(
        messages,
        (claim,),
        tool_call_ids_by_source={"get_indicators:NVDA:2026-01-15": ("tool-1",)},
    )

    assert evidence.claim_validations[0].status is ClaimValidationStatus.UNSUPPORTED
    assert "20" in evidence.claim_validations[0].detail


@pytest.mark.unit
def test_atomic_claims_with_their_own_quotes_remain_supported():
    messages = _tool_exchange(
        "get_indicators",
        {"ticker": "NVDA", "curr_date": "2026-01-15", "indicator": "rsi"},
        "Opening price was 10.\nClosing price was 20.",
        "tool-1",
    )
    claims = (
        MaterialClaim(
            claim_id="opening", analyst="market", statement="The opening price was 10.",
            source_refs=("get_indicators:NVDA:2026-01-15",),
            source_quote="Opening price was 10.",
        ),
        MaterialClaim(
            claim_id="closing", analyst="market", statement="The closing price was 20.",
            source_refs=("get_indicators:NVDA:2026-01-15",),
            source_quote="Closing price was 20.",
        ),
    )

    evidence = build_tool_evidence_state(
        messages,
        claims,
        tool_call_ids_by_source={"get_indicators:NVDA:2026-01-15": ("tool-1",)},
    )

    assert all(item.status is ClaimValidationStatus.SUPPORTED for item in evidence.claim_validations)


@pytest.mark.unit
def test_partial_data_degradation_does_not_hide_substantive_tool_evidence():
    messages = _tool_exchange(
        "get_fundamentals",
        {"ticker": "600895.SS", "curr_date": "2026-07-19"},
        (
            "PE Ratio (TTM): 61.77551\n\n"
            "## Degraded Fields\n"
            "DATA_DEGRADED: optional fund flow unavailable."
        ),
        "fundamentals-1",
    )
    claim = MaterialClaim(
        claim_id="fundamentals.pe",
        analyst="fundamentals",
        statement="The PE ratio is 61.77551.",
        source_quote="PE Ratio (TTM): 61.77551",
        source_refs=("get_fundamentals:600895.SS:2026-07-19",),
    )

    evidence = build_tool_evidence_state(
        messages,
        (claim,),
        tool_call_ids_by_source={
            "get_fundamentals:600895.SS:2026-07-19": ("fundamentals-1",)
        },
    )

    assert evidence.sources == (
        EvidenceSource(
            source_id="get_fundamentals:600895.SS:2026-07-19",
            status=EvidenceStatus.AVAILABLE,
            required=False,
        ),
    )
    assert evidence.claim_validations[0].status is ClaimValidationStatus.SUPPORTED


@pytest.mark.unit
def test_verified_snapshot_id_is_used_as_the_exact_market_source_ref():
    snapshot_id = f"snapshot:{'b' * 64}"
    messages = _tool_exchange(
        "get_verified_market_snapshot",
        {"symbol": "600895.SS", "curr_date": "2026-07-19"},
        f"Close | 30.27\nSnapshot ID: {snapshot_id}",
        "snapshot-1",
    )

    refs = submission.source_ref_tool_call_ids(
        list(messages), "600895.SS", "2026-07-19"
    )

    assert refs == {snapshot_id: ("snapshot-1",)}


@pytest.mark.unit
def test_snapshot_history_rows_are_provenance_not_material_evidence():
    snapshot_id = f"snapshot:{'b' * 64}"
    messages = _tool_exchange(
        "get_verified_market_snapshot",
        {"symbol": "600895.SS", "curr_date": "2026-07-19"},
        f"- History rows: 1211\nSnapshot ID: {snapshot_id}",
        "snapshot-1",
    )
    claim = MaterialClaim(
        claim_id="market.history_rows",
        analyst="market",
        statement="The snapshot contains 1211 history rows.",
        source_refs=(snapshot_id,),
        source_quote="- History rows: 1211",
    )

    evidence = build_tool_evidence_state(
        messages,
        (claim,),
        tool_call_ids_by_source={snapshot_id: ("snapshot-1",)},
    )

    assert evidence.claim_validations[0].status is ClaimValidationStatus.UNSUPPORTED
    assert evidence.claim_validations[0].detail == (
        "source quote contains provenance metadata, not a material fact"
    )


@pytest.mark.unit
def test_history_requirement_is_derived_from_bound_facts_not_llm_inflation():
    fact = SourceFact(
        fact_id="fact:rsi",
        source_ref="snapshot:abc",
        tool_call_id="tool-1",
        artifact_sha256="digest",
        raw_text="RSI: 51",
        source_span_start=0,
        source_span_end=7,
        calculation_ids=("rsi",),
    )
    claim = MaterialClaim(
        claim_id="rsi",
        analyst="market",
        statement="RSI is 51.",
        source_quote="RSI: 51",
        source_refs=("snapshot:abc",),
        fact_ids=(fact.fact_id,),
        minimum_history_rows=200,
    )

    derived = submission._apply_deterministic_history_requirements(
        EvidenceState(material_claims=(claim,), source_facts=(fact,))
    )

    assert derived.material_claims[0].minimum_history_rows == 14


@pytest.mark.unit
def test_finalization_catalog_keeps_late_source_fact_after_preview_boundary():
    messages = _tool_exchange(
        "get_indicators",
        {"ticker": "NVDA", "curr_date": "2026-01-15", "indicator": "rsi"},
        "x" * 4_100 + "\nLate evidence: RSI is 51.",
        "tool-1",
    )
    prompt = submission._finalization_prompt(
        analyst="market", ticker="NVDA", trade_date="2026-01-15", draft="draft", messages=list(messages)
    )

    assert "Late evidence: RSI is 51." in prompt


@pytest.mark.unit
def test_finalization_catalog_keeps_each_tool_artifact_sharing_a_source_ref():
    first = _tool_exchange(
        "get_indicators",
        {"ticker": "NVDA", "curr_date": "2026-01-15", "indicator": "rsi"},
        "RSI is 51.",
        "tool-rsi",
    )
    second = _tool_exchange(
        "get_indicators",
        {"ticker": "NVDA", "curr_date": "2026-01-15", "indicator": "macd"},
        "MACD is -0.4.",
        "tool-macd",
    )

    prompt = submission._finalization_prompt(
        analyst="market",
        ticker="NVDA",
        trade_date="2026-01-15",
        draft="draft",
        messages=[*first, *second],
    )

    assert "RSI is 51." in prompt
    assert "MACD is -0.4." in prompt


@pytest.mark.unit
def test_snapshot_cache_reacquires_when_a_later_claim_needs_more_history(monkeypatch):
    config_module._config = copy.deepcopy(default_config.DEFAULT_CONFIG)
    set_config({"data_vendors": {"core_stock_apis": "only"}})
    calls = []

    def provider(_symbol, start_date, _end_date):
        calls.append(None)
        rows = 5 if start_date == "2026-01-01" else (10 if len(calls) == 1 else 20)
        dates = market_snapshot.pd.bdate_range(end="2026-01-15", periods=rows)
        return market_snapshot.pd.DataFrame({"Date": dates, "Open": [10.0] * rows, "High": [11.0] * rows, "Low": [9.0] * rows, "Close": [10.0] * rows, "Volume": [1] * rows})

    monkeypatch.setattr(market_snapshot, "SNAPSHOT_PROVIDERS", {"only": SnapshotProvider(provider, "qfq")})
    with authoritative_snapshot_run():
        get_authoritative_market_snapshot("NVDA", "2025-01-01", "2026-01-15")
        snapshot = get_authoritative_market_snapshot("NVDA", "2025-01-01", "2026-01-15", minimum_history_rows=20)

    assert len(calls) == 2
    assert len(snapshot.frame) == 20


@pytest.mark.unit
def test_reacquired_snapshot_becomes_shared_authority_for_analyst_claims(monkeypatch):
    config_module._config = copy.deepcopy(default_config.DEFAULT_CONFIG)
    set_config({"data_vendors": {"core_stock_apis": "only"}})
    calls = []

    def provider(_symbol, start_date, _end_date):
        calls.append(None)
        rows = 5 if start_date == "2026-01-01" else (10 if len(calls) == 1 else 20)
        dates = market_snapshot.pd.bdate_range(end="2026-01-15", periods=rows)
        return market_snapshot.pd.DataFrame(
            {
                "Date": dates,
                "Open": [10.0] * rows,
                "High": [11.0] * rows,
                "Low": [9.0] * rows,
                "Close": [10.0] * rows,
                "Volume": [1] * rows,
            }
        )

    monkeypatch.setattr(
        market_snapshot,
        "SNAPSHOT_PROVIDERS",
        {"only": SnapshotProvider(provider, "qfq")},
    )
    with authoritative_snapshot_run():
        initial = get_authoritative_market_snapshot(
            "NVDA", "2025-01-01", "2026-01-15"
        )
        reacquired = get_authoritative_market_snapshot(
            "NVDA",
            "2025-01-01",
            "2026-01-15",
            minimum_history_rows=20,
        )
        shorter = get_authoritative_market_snapshot(
            "NVDA", "2026-01-01", "2026-01-15"
        )
        assert len(shorter.frame) == 5
        messages = list(
            _tool_exchange(
                "get_indicators",
                {
                    "symbol": "NVDA",
                    "curr_date": "2026-01-15",
                    "indicator": "rsi",
                },
                f"RSI is 51.\nSnapshot ID: {reacquired.snapshot_id}",
                "tool-rsi",
            )
        )
        claim = MaterialClaim(
            claim_id="market.rsi",
            analyst="market",
            statement="RSI is 51.",
            source_quote="RSI is 51.",
            source_refs=(reacquired.snapshot_id,),
        )
        result = submission.AnalystSubmissionResult(
            message=AIMessage(content="finalized"),
            report="Market report",
            claims=(claim,),
            submission_source=EvidenceSource(
                source_id="analyst.market.submission",
                status=EvidenceStatus.AVAILABLE,
                required=True,
            ),
        )
        update = submission.build_analyst_update(
            {
                "company_of_interest": "NVDA",
                "trade_date": "2026-01-15",
                "messages": messages,
                "evidence_state": EvidenceState(
                    instrument_identity=InstrumentIdentityEvidence(
                        symbol="NVDA", name="NVIDIA"
                    ),
                    market_snapshot=MarketSnapshotEvidence(
                        symbol=initial.symbol,
                        provider=initial.provider,
                        retrieved_at=initial.retrieved_at,
                        adjustment_basis=initial.adjustment_basis,
                        requested_date=initial.requested_date,
                        effective_trading_date=initial.effective_trading_date,
                        history_rows=len(initial.frame),
                        frame_sha256=initial.frame_sha256,
                        snapshot_id=initial.snapshot_id,
                    ),
                ).model_dump(mode="json"),
            },
            result,
            "market_report",
        )

    evidence = EvidenceState.model_validate(update["evidence_state"])
    assert evidence.market_snapshot is not None
    assert evidence.market_snapshot.snapshot_id == reacquired.snapshot_id
    assert tuple(item.claim_id for item in decision_ready_material_claims(evidence)) == (
        "market.rsi",
    )


@pytest.mark.unit
def test_evidence_coverage_counts_an_authoritative_snapshot_once():
    snapshot = MarketSnapshotEvidence(symbol="NVDA", provider="test", retrieved_at="now", adjustment_basis="qfq", requested_date="2026-01-15", effective_trading_date="2026-01-15", history_rows=10, snapshot_id="snapshot:abc")
    evidence = EvidenceState(
        market_snapshot=snapshot,
        sources=(
            EvidenceSource(source_id="snapshot:abc", status=EvidenceStatus.AVAILABLE, required=False),
            EvidenceSource(source_id="snapshot:abc:indicators", status=EvidenceStatus.AVAILABLE, required=False),
        ),
    )

    assert _evidence_coverage(evidence) == pytest.approx(1 / 2)

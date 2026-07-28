from __future__ import annotations

import copy
from datetime import date, timedelta
from decimal import Decimal
from hashlib import sha256
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pandas as pd
import pytest

import tradingagents.dataflows.config as config_module
import tradingagents.default_config as default_config
import tradingagents.graph.trading_graph as trading_graph_module
from tradingagents.agents.managers.direction_selector import create_decision_gate_node
from tradingagents.agents.schemas import PortfolioRating
from tradingagents.asset_configuration import resolve_run_asset_configuration
from tradingagents.dataflows import market_snapshot
from tradingagents.dataflows.market_snapshot import (
    SnapshotProvider,
    authoritative_snapshot_run,
)
from tradingagents.decision_policy import DirectionSelection
from tradingagents.evidence import (
    AcquisitionUnavailableReason,
    EvidenceState,
    IdentityProvenance,
    InstrumentIdentityEvidence,
    InstrumentKind,
    MarketSnapshotEvidence,
    SourceAcquisitionUnavailable,
    SourceArtifact,
    acquire_run_evidence,
    stable_market_snapshot_id,
    stable_source_fact_id,
)
from tradingagents.strategy_registry import (
    CRYPTO_DECISION_HORIZON,
    DEFAULT_DECISION_HORIZON,
    InsufficientStrategyHistoryError,
    build_market_return_fact,
    calculate_market_return,
    create_production_decision_policy,
    market_return_calculation_definitions,
)


def _market_evidence(
    last_close: str = "110",
    *,
    instrument_kind: str = "equity",
    symbol: str = "600895.SS",
) -> EvidenceState:
    dates = tuple(f"2026-06-{day:02d}" for day in range(1, 22))
    closes = ("100",) * 20 + (last_close,)
    raw_text = "Date,Open,High,Low,Close,Volume\n" + "".join(
        f"{trade_date},{close},{close},{close},{close},100\n"
        for trade_date, close in zip(dates, closes, strict=True)
    )


    digest = sha256(raw_text.encode("utf-8")).hexdigest()
    artifact = SourceArtifact(
        artifact_sha256=digest,
        source_ref="acq.v1:market:test",
        tool_call_id="market-call-1",
        tool_name="authoritative_market_snapshot_normalized_frame_v1",
        raw_text=raw_text,
    )
    snapshot_id = stable_market_snapshot_id(
        symbol=symbol,
        provider="synthetic",
        adjustment_basis="qfq",
        requested_date="2026-06-21",
        effective_trading_date="2026-06-21",
        frame_sha256=digest,
        history_rows=21,
    )
    snapshot = SimpleNamespace(
        adjustment_basis="qfq",
        symbol=symbol,
        effective_trading_date="2026-06-21",
        snapshot_id=snapshot_id,
    )
    fact = build_market_return_fact(snapshot, artifact)
    return EvidenceState(
        instrument_identity=InstrumentIdentityEvidence(
            symbol=symbol,
            venue="XSHG",
            instrument_kind=instrument_kind,
            currency="CNY",
            provenance=IdentityProvenance(
                provider="mainland-security-master",
                source_ref="security-master:510500.SS",
                retrieved_at="2026-06-21T12:00:00+00:00",
                artifact_sha256="a" * 64,
            ),
        ),
        market_snapshot=MarketSnapshotEvidence(
            symbol=symbol,
            provider="synthetic",
            retrieved_at="2026-06-21T12:00:00+00:00",
            adjustment_basis="qfq",
            requested_date="2026-06-21",
            effective_trading_date="2026-06-21",
            history_rows=21,
            frame_sha256=digest,
            snapshot_id=snapshot.snapshot_id,
        ),
        source_facts=(fact,),
        source_artifacts=(artifact,),
    )


@pytest.mark.unit
def test_market_return_definitions_name_instrument_specific_calendars():
    definitions = market_return_calculation_definitions()
    equity = tuple(
        definition
        for definition in definitions
        if definition.applicable_instrument_kinds is not None
        and InstrumentKind.EQUITY in definition.applicable_instrument_kinds
    )
    crypto = tuple(
        definition
        for definition in definitions
        if definition.applicable_instrument_kinds == (InstrumentKind.CRYPTO,)
    )

    assert len(equity) == 2
    assert {definition.input_frequency for definition in equity} == {"trading_day"}
    assert len(crypto) == 2
    assert {definition.input_frequency for definition in crypto} == {"calendar_day"}
    assert CRYPTO_DECISION_HORIZON.count == 20
    assert CRYPTO_DECISION_HORIZON.unit.value == "calendar_days"


@pytest.mark.unit
def test_crypto_market_return_uses_21_consecutive_calendar_closes():
    dates = tuple(date(2026, 6, 1) + timedelta(days=offset) for offset in range(21))
    closes = ("100",) * 20 + ("110",)
    raw_text = "Date,Open,High,Low,Close,Volume\n" + "".join(
        f"{day.isoformat()},{close},{close},{close},{close},100\n"
        for day, close in zip(dates, closes, strict=True)
    )

    value = calculate_market_return(raw_text, instrument_kind=InstrumentKind.CRYPTO)

    assert value == Decimal("0.10000000")


@pytest.mark.unit
def test_crypto_market_return_reports_missing_calendar_date_as_insufficient_history():
    dates = tuple(
        date(2026, 6, 1) + timedelta(days=offset)
        for offset in (*range(20), 21)
    )
    raw_text = "Date,Open,High,Low,Close,Volume\n" + "".join(
        f"{day.isoformat()},100,100,100,100,100\n" for day in dates
    )

    with pytest.raises(
        InsufficientStrategyHistoryError,
        match="not consecutive calendar days",
    ):
        calculate_market_return(raw_text, instrument_kind=InstrumentKind.CRYPTO)


@pytest.mark.unit
def test_crypto_market_return_requires_all_21_daily_closes():
    dates = tuple(date(2026, 6, 1) + timedelta(days=offset) for offset in range(21))
    closes = ("100",) * 10 + ("",) + ("100",) * 10
    raw_text = "Date,Open,High,Low,Close,Volume\n" + "".join(
        f"{day.isoformat()},100,100,100,{close},100\n"
        for day, close in zip(dates, closes, strict=True)
    )

    with pytest.raises(
        InsufficientStrategyHistoryError,
        match="21 valid daily closes",
    ):
        calculate_market_return(raw_text, instrument_kind=InstrumentKind.CRYPTO)


@pytest.mark.unit
def test_equity_market_return_preserves_market_session_semantics():
    dates = tuple(
        date(2026, 6, 1) + timedelta(days=offset)
        for offset in range(29)
        if (date(2026, 6, 1) + timedelta(days=offset)).weekday() < 5
    )[:21]
    closes = ("100",) * 20 + ("110",)
    raw_text = "Date,Open,High,Low,Close,Volume\n" + "".join(
        f"{day.isoformat()},{close},{close},{close},{close},100\n"
        for day, close in zip(dates, closes, strict=True)
    )

    value = calculate_market_return(raw_text, instrument_kind=InstrumentKind.EQUITY)

    assert value == Decimal("0.10000000")


@pytest.mark.unit
def test_production_policy_registers_separate_crypto_calendar_rules():
    policy = create_production_decision_policy()
    crypto_rules = tuple(
        rule
        for rule in policy.rules
        if InstrumentKind.CRYPTO in rule.applicable_instrument_kinds
    )

    assert len(crypto_rules) == 3
    assert {rule.horizon for rule in crypto_rules} == {CRYPTO_DECISION_HORIZON}
    assert policy.preflight_minimum_history_rows(
        InstrumentKind.CRYPTO,
        CRYPTO_DECISION_HORIZON,
    ) == 21
    assert (
        policy.preflight_minimum_history_rows(
            InstrumentKind.CRYPTO,
            DEFAULT_DECISION_HORIZON,
        )
        is None
    )


@pytest.mark.unit
def test_crypto_fact_uses_calendar_definition_and_crypto_rules():
    dates = tuple(date(2026, 6, 1) + timedelta(days=offset) for offset in range(21))
    closes = ("100",) * 20 + ("110",)
    raw_text = "Date,Open,High,Low,Close,Volume\n" + "".join(
        f"{day.isoformat()},{close},{close},{close},{close},100\n"
        for day, close in zip(dates, closes, strict=True)
    )
    digest = sha256(raw_text.encode("utf-8")).hexdigest()
    artifact = SourceArtifact(
        artifact_sha256=digest,
        source_ref="acq.v1:market:crypto-test",
        tool_call_id="crypto-market-call",
        tool_name="authoritative_market_snapshot_normalized_frame_v1",
        raw_text=raw_text,
    )
    snapshot = SimpleNamespace(
        adjustment_basis="auto_adjusted",
        symbol="BTC-USD",
        effective_trading_date="2026-06-21",
        snapshot_id="snapshot:crypto-test",
        history_gap_dates=(),
    )

    fact = build_market_return_fact(
        snapshot,
        artifact,
        instrument_kind=InstrumentKind.CRYPTO,
    )

    assert fact.calculation_lineage is not None
    assert fact.calculation_lineage.calculation_id == (
        "market.close_return_20d.crypto.auto_adjusted"
    )
    assert fact.calculation_lineage.effective_range_start == "2026-06-01"
    assert fact.calculation_lineage.effective_range_end == "2026-06-21"
    assert fact.calculation_lineage.observations_used == 21
    evidence = EvidenceState(
        instrument_identity=InstrumentIdentityEvidence(
            symbol="BTC-USD",
            venue="CCC",
            instrument_kind=InstrumentKind.CRYPTO,
            currency="USD",
            provenance=IdentityProvenance(
                provider="Yahoo Finance",
                source_ref="https://finance.yahoo.com/quote/BTC-USD/",
                retrieved_at="2026-06-21T12:00:00+00:00",
                artifact_sha256="a" * 64,
            ),
        ),
        market_snapshot=MarketSnapshotEvidence(
            symbol="BTC-USD",
            provider="yfinance",
            retrieved_at="2026-06-21T12:00:00+00:00",
            adjustment_basis="auto_adjusted",
            requested_date="2026-06-21",
            effective_trading_date="2026-06-21",
            history_rows=21,
            frame_sha256=digest,
            snapshot_id=snapshot.snapshot_id,
        ),
        source_facts=(fact,),
        source_artifacts=(artifact,),
    )
    policy = create_production_decision_policy()

    applications = policy.candidate_applications(
        evidence,
        horizon=CRYPTO_DECISION_HORIZON,
    )

    assert {application.rule_id for application in applications} == {
        "market.return_20d.crypto.buy",
        "market.return_20d.crypto.hold",
        "market.return_20d.crypto.sell",
    }


@pytest.mark.unit
def test_crypto_run_evidence_uses_crypto_calendar_calculation(monkeypatch):
    registry_path = (
        Path(__file__).parents[1] / "config" / "crypto_identity_registry.json"
    )
    registry_digest = registry_path.with_suffix(".sha256").read_text(
        encoding="utf-8"
    ).split()[0]
    config = copy.deepcopy(default_config.DEFAULT_CONFIG)
    config.update(
        {
            "crypto_identity_registry_path": str(registry_path),
            "crypto_identity_registry_sha256": registry_digest,
        }
    )
    monkeypatch.setattr(config_module, "_config", config)
    dates = pd.date_range(start="2026-06-01", periods=21, freq="D")
    closes = [100.0] * 20 + [110.0]
    frame = pd.DataFrame(
        {
            "Date": dates,
            "Open": closes,
            "High": closes,
            "Low": closes,
            "Close": closes,
            "Volume": [100.0] * 21,
        }
    )

    with (
        patch.object(
            market_snapshot,
            "SNAPSHOT_PROVIDERS",
            {"yfinance": SnapshotProvider(lambda *_args: frame, "auto_adjusted")},
        ),
        authoritative_snapshot_run(),
    ):
        evidence = acquire_run_evidence("BTC-USD", "2026-06-21")

    fact = evidence.source_facts[0]
    assert fact.calculation_lineage is not None
    assert fact.calculation_lineage.calculation_id == (
        "market.close_return_20d.crypto.auto_adjusted"
    )


@pytest.mark.unit
def test_crypto_run_evidence_classifies_missing_date_as_insufficient_history(
    monkeypatch,
):
    registry_path = (
        Path(__file__).parents[1] / "config" / "crypto_identity_registry.json"
    )
    registry_digest = registry_path.with_suffix(".sha256").read_text(
        encoding="utf-8"
    ).split()[0]
    config = copy.deepcopy(default_config.DEFAULT_CONFIG)
    config.update(
        {
            "crypto_identity_registry_path": str(registry_path),
            "crypto_identity_registry_sha256": registry_digest,
        }
    )
    monkeypatch.setattr(config_module, "_config", config)
    dates = tuple(
        pd.Timestamp("2026-06-01") + pd.Timedelta(days=offset)
        for offset in (*range(20), 21)
    )
    frame = pd.DataFrame(
        {
            "Date": dates,
            "Open": [100.0] * 21,
            "High": [100.0] * 21,
            "Low": [100.0] * 21,
            "Close": [100.0] * 21,
            "Volume": [100.0] * 21,
        }
    )

    with (
        patch.object(
            market_snapshot,
            "SNAPSHOT_PROVIDERS",
            {"yfinance": SnapshotProvider(lambda *_args: frame, "auto_adjusted")},
        ),
        authoritative_snapshot_run(),
    ):
        evidence = acquire_run_evidence("BTC-USD", "2026-06-22")

    outcome = next(
        candidate
        for candidate in evidence.acquisition_outcomes
        if candidate.capability == "market_return_20d"
    )
    assert isinstance(outcome, SourceAcquisitionUnavailable)
    assert outcome.reason is AcquisitionUnavailableReason.INSUFFICIENT_HISTORY
    assert outcome.calculation_readiness is not None
    assert outcome.calculation_readiness.calculation_id == (
        "market.close_return_20d.crypto.auto_adjusted"
    )
    assert evidence.source_facts == ()


@pytest.mark.unit
def test_crypto_policy_revalidates_calendar_continuity_from_source_artifact():
    valid_dates = tuple(
        date(2026, 6, 1) + timedelta(days=offset) for offset in range(21)
    )
    valid_raw = "Date,Open,High,Low,Close,Volume\n" + "".join(
        f"{day.isoformat()},100,100,100,100,100\n" for day in valid_dates
    )
    valid_digest = sha256(valid_raw.encode("utf-8")).hexdigest()
    valid_artifact = SourceArtifact(
        artifact_sha256=valid_digest,
        source_ref="acq.v1:market:crypto-adapter-test",
        tool_call_id="crypto-adapter-call",
        tool_name="authoritative_market_snapshot_normalized_frame_v1",
        raw_text=valid_raw,
    )
    valid_snapshot_id = stable_market_snapshot_id(
        symbol="BTC-USD",
        provider="synthetic",
        adjustment_basis="auto_adjusted",
        requested_date="2026-06-21",
        effective_trading_date="2026-06-21",
        frame_sha256=valid_digest,
        history_rows=21,
    )
    fact = build_market_return_fact(
        SimpleNamespace(
            adjustment_basis="auto_adjusted",
            symbol="BTC-USD",
            effective_trading_date="2026-06-21",
            snapshot_id=valid_snapshot_id,
            history_gap_dates=(),
        ),
        valid_artifact,
        instrument_kind=InstrumentKind.CRYPTO,
    )
    missing_date_rows = tuple(
        date(2026, 6, 1) + timedelta(days=offset)
        for offset in (*range(20), 21)
    )
    missing_date_raw = "Date,Open,High,Low,Close,Volume\n" + "".join(
        f"{day.isoformat()},100,100,100,100,100\n" for day in missing_date_rows
    )
    missing_date_digest = sha256(missing_date_raw.encode("utf-8")).hexdigest()
    missing_date_snapshot_id = stable_market_snapshot_id(
        symbol="BTC-USD",
        provider="synthetic",
        adjustment_basis="auto_adjusted",
        requested_date="2026-06-22",
        effective_trading_date="2026-06-22",
        frame_sha256=missing_date_digest,
        history_rows=21,
    )
    assert fact.calculation_lineage is not None
    bad_lineage = fact.calculation_lineage.model_copy(
        update={
            "input_artifact_sha256": missing_date_digest,
            "input_snapshot_id": missing_date_snapshot_id,
            "effective_range_end": "2026-06-22",
        }
    )
    bad_fact = fact.model_copy(
        update={
            "fact_id": stable_source_fact_id(
                source_ref=fact.source_ref,
                artifact_sha256=missing_date_digest,
                source_span_start=fact.source_span_start,
                source_span_end=len(missing_date_raw),
                canonical_field=fact.canonical_field,
                instrument_symbol=fact.instrument_symbol,
                effective_date="2026-06-22",
            ),
            "artifact_sha256": missing_date_digest,
            "raw_text": missing_date_raw[fact.source_span_start :],
            "source_span_end": len(missing_date_raw),
            "effective_date": "2026-06-22",
            "calculation_lineage": bad_lineage,
        }
    )
    evidence = EvidenceState(
        instrument_identity=InstrumentIdentityEvidence(
            symbol="BTC-USD",
            venue="CCC",
            instrument_kind=InstrumentKind.CRYPTO,
            currency="USD",
            provenance=IdentityProvenance(
                provider="Yahoo Finance",
                source_ref="https://finance.yahoo.com/quote/BTC-USD/",
                retrieved_at="2026-06-22T12:00:00+00:00",
                artifact_sha256="a" * 64,
            ),
        ),
        market_snapshot=MarketSnapshotEvidence(
            symbol="BTC-USD",
            provider="synthetic",
            retrieved_at="2026-06-22T12:00:00+00:00",
            adjustment_basis="auto_adjusted",
            requested_date="2026-06-22",
            effective_trading_date="2026-06-22",
            history_rows=21,
            frame_sha256=missing_date_digest,
            snapshot_id=missing_date_snapshot_id,
        ),
        source_facts=(bad_fact,),
        source_artifacts=(
            valid_artifact.model_copy(
                update={
                    "artifact_sha256": missing_date_digest,
                    "raw_text": missing_date_raw,
                }
            ),
        ),
    )
    policy = create_production_decision_policy()
    applications = policy.candidate_applications(
        evidence,
        horizon=CRYPTO_DECISION_HORIZON,
    )

    result = policy.build_context(
        evidence,
        applications,
        horizon=CRYPTO_DECISION_HORIZON,
        as_of_date=date(2026, 6, 22),
        tolerate_unsatisfied_applications=True,
    )

    assert result.kind == "blocked"
    assert "canonical_fact_adapter_failed" in result.blocker_codes


@pytest.mark.unit
def test_market_return_fact_rejects_history_gap_inside_registered_window() -> None:
    raw_text = "Date,Open,High,Low,Close,Volume\n" + "".join(
        f"2026-06-{day:02d},100,100,100,100,100\n" for day in range(1, 22)
    )
    artifact = SourceArtifact(
        artifact_sha256=sha256(raw_text.encode("utf-8")).hexdigest(),
        source_ref="acq.v1:market:gap-test",
        tool_call_id="market-gap-call",
        tool_name="authoritative_market_snapshot_normalized_frame_v1",
        raw_text=raw_text,
    )
    snapshot = SimpleNamespace(
        adjustment_basis="qfq",
        symbol="600895.SS",
        effective_trading_date="2026-06-21",
        snapshot_id="snapshot:gap-test",
        history_gap_dates=("2026-06-10",),
    )

    with pytest.raises(ValueError, match="History Gap"):
        build_market_return_fact(snapshot, artifact)


@pytest.mark.unit
@pytest.mark.parametrize("instrument_kind", ("fund", "index"))
def test_generic_market_return_rules_do_not_apply_to_non_equity_instruments(
    instrument_kind,
):
    symbol = "510500.SS" if instrument_kind == "fund" else "000001.SS"
    evidence = _market_evidence(
        instrument_kind=instrument_kind,
        symbol=symbol,
    )
    policy = create_production_decision_policy()

    applications = policy.candidate_applications(
        evidence,
        horizon=DEFAULT_DECISION_HORIZON,
    )

    assert applications == ()


@pytest.mark.unit
@pytest.mark.parametrize(
    ("last_close", "expected_rating", "expected_rule_id"),
    [
        ("105", PortfolioRating.BUY, "market.return_20d.buy"),
        ("104.999999", PortfolioRating.HOLD, "market.return_20d.hold"),
        ("95", PortfolioRating.SELL, "market.return_20d.sell"),
    ],
)
def test_production_policy_authorizes_rule_supported_rating_from_snapshot(
    last_close,
    expected_rating,
    expected_rule_id,
):
    evidence = _market_evidence(last_close)
    policy = create_production_decision_policy()
    applications = policy.candidate_applications(
        evidence,
        horizon=DEFAULT_DECISION_HORIZON,
    )

    built = policy.build_context(
        evidence,
        applications,
        horizon=DEFAULT_DECISION_HORIZON,
        as_of_date=date(2026, 6, 21),
        tolerate_unsatisfied_applications=True,
    )
    context = built.context
    binding = policy.admit_evidence(
        evidence,
        context,
        run_id="run:" + "1" * 64,
    )
    selection = DirectionSelection(
        context_id=context.context_id,
        rating=expected_rating,
        assertion_ids=tuple(assertion.assertion_id for assertion in context.assertions),
    )

    result = policy.gate(
        context,
        selection,
        evidence=evidence,
        admitted_evidence_binding=binding,
        run_id=binding.run_id,
        expected_run_id=binding.run_id,
    )

    assert result.permitted is True
    assert result.decision is not None
    assert result.decision.rating is expected_rating
    assert tuple(assertion.rule_id for assertion in result.decision.assertions) == (
        expected_rule_id,
    )


@pytest.mark.unit
def test_graph_decision_gate_authorizes_from_checkpoint_safe_artifact_ledger():
    evidence = _market_evidence()
    policy = create_production_decision_policy()
    built = policy.build_context(
        evidence,
        policy.candidate_applications(
            evidence,
            horizon=DEFAULT_DECISION_HORIZON,
        ),
        horizon=DEFAULT_DECISION_HORIZON,
        as_of_date=date(2026, 6, 21),
        tolerate_unsatisfied_applications=True,
    )
    context = built.context
    binding = policy.admit_evidence(
        evidence,
        context,
        run_id="run:" + "2" * 64,
    )
    selection = DirectionSelection(
        context_id=context.context_id,
        rating=PortfolioRating.BUY,
        assertion_ids=tuple(assertion.assertion_id for assertion in context.assertions),
    )
    node = create_decision_gate_node(policy)

    update = node(
        {
            "run_id": binding.run_id,
            "admitted_evidence_binding": binding.model_dump(mode="json"),
            "validated_decision_context": context.model_dump(mode="json"),
            "direction_selection": selection.model_dump(mode="json"),
            "evidence_state": evidence.model_dump(mode="json"),
        },
        config={"configurable": {"run_id": binding.run_id}},
    )

    assert update["decision_gate"]["permitted"] is True
    assert update["trading_decision"]["rating"] == PortfolioRating.BUY.value
    assert "Analysis Outcome" not in update.get("analysis_outcome", "")


def test_fresh_policy_instance_authorizes_from_checkpointed_admission_binding():
    evidence = _market_evidence()
    admission_policy = create_production_decision_policy()
    built = admission_policy.build_context(
        evidence,
        admission_policy.candidate_applications(
            evidence,
            horizon=DEFAULT_DECISION_HORIZON,
        ),
        horizon=DEFAULT_DECISION_HORIZON,
        as_of_date=date(2026, 6, 21),
        tolerate_unsatisfied_applications=True,
    )
    context = built.context
    binding = admission_policy.admit_evidence(
        evidence,
        context,
        run_id="run:" + "3" * 64,
    )
    selection = DirectionSelection(
        context_id=context.context_id,
        rating=PortfolioRating.BUY,
        assertion_ids=tuple(
            assertion.assertion_id for assertion in context.assertions
        ),
    )

    resumed_node = create_decision_gate_node(create_production_decision_policy())
    update = resumed_node(
        {
            "run_id": binding.run_id,
            "admitted_evidence_binding": binding.model_dump(mode="json"),
            "validated_decision_context": context.model_dump(mode="json"),
            "direction_selection": selection.model_dump(mode="json"),
            "evidence_state": evidence.model_dump(mode="json"),
        },
        config={"configurable": {"run_id": binding.run_id}},
    )

    assert update["decision_gate"]["permitted"] is True
    assert update["trading_decision"]["rating"] == PortfolioRating.BUY.value


def test_decision_gate_rejects_legacy_state_without_admission_binding():
    evidence = _market_evidence()
    policy = create_production_decision_policy()
    built = policy.build_context(
        evidence,
        policy.candidate_applications(
            evidence,
            horizon=DEFAULT_DECISION_HORIZON,
        ),
        horizon=DEFAULT_DECISION_HORIZON,
        as_of_date=date(2026, 6, 21),
        tolerate_unsatisfied_applications=True,
    )
    context = built.context
    selection = DirectionSelection(
        context_id=context.context_id,
        rating=PortfolioRating.BUY,
        assertion_ids=tuple(
            assertion.assertion_id for assertion in context.assertions
        ),
    )

    update = create_decision_gate_node(policy)(
        {
            "validated_decision_context": context.model_dump(mode="json"),
            "direction_selection": selection.model_dump(mode="json"),
            "evidence_state": evidence.model_dump(mode="json"),
        }
    )

    assert update["decision_gate"]["permitted"] is False
    assert "admitted evidence binding is unavailable" in update["decision_gate"][
        "diagnostics"
    ]


@pytest.mark.unit
def test_graph_decision_gate_blocks_without_registered_source_fact_ledger():
    evidence = _market_evidence()
    policy = create_production_decision_policy()
    built = policy.build_context(
        evidence,
        policy.candidate_applications(
            evidence,
            horizon=DEFAULT_DECISION_HORIZON,
        ),
        horizon=DEFAULT_DECISION_HORIZON,
        as_of_date=date(2026, 6, 21),
        tolerate_unsatisfied_applications=True,
    )
    context = built.context
    selection = DirectionSelection(
        context_id=context.context_id,
        rating=PortfolioRating.BUY,
        assertion_ids=tuple(assertion.assertion_id for assertion in context.assertions),
    )
    node = create_decision_gate_node(policy)
    update = node({
        "validated_decision_context": context.model_dump(mode="json"),
        "direction_selection": selection.model_dump(mode="json"),
        "evidence_state": evidence.model_dump(mode="json"),
    })

    assert update["decision_gate"]["permitted"] is False
    assert update["decision_gate"]["diagnostics"] == [
        "admitted evidence binding is unavailable"
    ]
    assert "trading_decision" not in update


@pytest.mark.unit
def test_graph_decision_gate_blocks_changed_source_fact_identity():
    evidence = _market_evidence()
    policy = create_production_decision_policy()
    built = policy.build_context(
        evidence,
        policy.candidate_applications(
            evidence,
            horizon=DEFAULT_DECISION_HORIZON,
        ),
        horizon=DEFAULT_DECISION_HORIZON,
        as_of_date=date(2026, 6, 21),
        tolerate_unsatisfied_applications=True,
    )
    context = built.context
    binding = policy.admit_evidence(
        evidence,
        context,
        run_id="run:" + "4" * 64,
    )
    original_fact = evidence.source_facts[0]
    changed_fact = original_fact.model_copy(update={"effective_date": "2026-06-20"})
    changed_fact = changed_fact.model_copy(
        update={
            "fact_id": stable_source_fact_id(
                source_ref=changed_fact.source_ref,
                artifact_sha256=changed_fact.artifact_sha256,
                source_span_start=changed_fact.source_span_start,
                source_span_end=changed_fact.source_span_end,
                canonical_field=changed_fact.canonical_field,
                instrument_symbol=changed_fact.instrument_symbol,
                effective_date=changed_fact.effective_date,
            )
        }
    )
    changed_evidence = evidence.model_copy(update={"source_facts": (changed_fact,)})
    selection = DirectionSelection(
        context_id=context.context_id,
        rating=PortfolioRating.BUY,
        assertion_ids=tuple(assertion.assertion_id for assertion in context.assertions),
    )
    node = create_decision_gate_node(policy)

    update = node(
        {
            "run_id": binding.run_id,
            "admitted_evidence_binding": binding.model_dump(mode="json"),
            "validated_decision_context": context.model_dump(mode="json"),
            "direction_selection": selection.model_dump(mode="json"),
            "evidence_state": changed_evidence.model_dump(mode="json"),
        },
        config={"configurable": {"run_id": binding.run_id}},
    )

    assert update["decision_gate"]["permitted"] is False
    assert update["decision_gate"]["diagnostics"] == [
        "decision admitted evidence mismatch"
    ]
    assert "trading_decision" not in update


@pytest.mark.unit
def test_normal_graph_construction_uses_shipped_policy_and_horizon(
    tmp_path,
    monkeypatch,
):
    llm = MagicMock()
    llm.with_structured_output.return_value = MagicMock()
    client = MagicMock()
    client.get_llm.return_value = llm
    monkeypatch.setattr(
        trading_graph_module,
        "create_llm_client",
        lambda **_kwargs: client,
    )
    monkeypatch.setattr(
        config_module,
        "_config",
        copy.deepcopy(default_config.DEFAULT_CONFIG),
    )
    config = copy.deepcopy(default_config.DEFAULT_CONFIG)
    config.update({
        "results_dir": str(tmp_path / "results"),
        "data_cache_dir": str(tmp_path / "cache"),
        "memory_log_path": str(tmp_path / "memory.md"),
    })

    graph = trading_graph_module.TradingAgentsGraph(
        selected_analysts=("market",),
        config=config,
    )

    assert graph.decision_policy.configuration_blockers == ()
    assert graph.decision_horizon == DEFAULT_DECISION_HORIZON


@pytest.mark.unit
def test_crypto_graph_construction_excludes_fundamentals_and_uses_calendar_horizon(
    tmp_path,
    monkeypatch,
):
    llm = MagicMock()
    llm.with_structured_output.return_value = MagicMock()
    client = MagicMock()
    client.get_llm.return_value = llm
    monkeypatch.setattr(
        trading_graph_module,
        "create_llm_client",
        lambda **_kwargs: client,
    )
    monkeypatch.setattr(
        config_module,
        "_config",
        copy.deepcopy(default_config.DEFAULT_CONFIG),
    )
    config = copy.deepcopy(default_config.DEFAULT_CONFIG)
    config.update(
        {
            "results_dir": str(tmp_path / "results"),
            "data_cache_dir": str(tmp_path / "cache"),
            "memory_log_path": str(tmp_path / "memory.md"),
        }
    )

    graph = trading_graph_module.TradingAgentsGraph(
        selected_analysts=("market", "social", "news", "fundamentals"),
        asset_type="crypto",
        asset_configuration=resolve_run_asset_configuration(
            "SOL-USD",
            config=config,
        ),
        config=config,
    )

    assert graph.selected_analysts == ("market", "social", "news")
    assert graph.decision_horizon == CRYPTO_DECISION_HORIZON
    assert "Fundamentals Analyst" not in graph.workflow.nodes
    assert "tools_fundamentals" not in graph.workflow.nodes

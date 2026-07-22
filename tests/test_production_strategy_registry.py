from __future__ import annotations

import copy
from datetime import date
from hashlib import sha256
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

import tradingagents.dataflows.config as config_module
import tradingagents.default_config as default_config
import tradingagents.graph.trading_graph as trading_graph_module
from tradingagents.agents.managers.direction_selector import create_decision_gate_node
from tradingagents.agents.schemas import PortfolioRating
from tradingagents.decision_policy import DirectionSelection
from tradingagents.evidence import (
    EvidenceState,
    IdentityProvenance,
    InstrumentIdentityEvidence,
    MarketSnapshotEvidence,
    SourceArtifact,
    stable_market_snapshot_id,
    stable_source_fact_id,
)
from tradingagents.strategy_registry import (
    DEFAULT_DECISION_HORIZON,
    build_market_return_fact,
    create_production_decision_policy,
)


def _market_evidence(last_close: str = "110") -> EvidenceState:
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
        symbol="510500.SS",
        provider="synthetic",
        adjustment_basis="qfq",
        requested_date="2026-06-21",
        effective_trading_date="2026-06-21",
        frame_sha256=digest,
        history_rows=21,
    )
    snapshot = SimpleNamespace(
        adjustment_basis="qfq",
        symbol="510500.SS",
        effective_trading_date="2026-06-21",
        snapshot_id=snapshot_id,
    )
    fact = build_market_return_fact(snapshot, artifact)
    return EvidenceState(
        instrument_identity=InstrumentIdentityEvidence(
            symbol="510500.SS",
            venue="XSHG",
            instrument_kind="fund",
            currency="CNY",
            provenance=IdentityProvenance(
                provider="mainland-security-master",
                source_ref="security-master:510500.SS",
                retrieved_at="2026-06-21T12:00:00+00:00",
                artifact_sha256="a" * 64,
            ),
        ),
        market_snapshot=MarketSnapshotEvidence(
            symbol="510500.SS",
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
    policy.register_trusted_evidence(evidence)
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
    policy.register_admitted_evidence(evidence, context)
    selection = DirectionSelection(
        context_id=context.context_id,
        rating=expected_rating,
        assertion_ids=tuple(assertion.assertion_id for assertion in context.assertions),
    )

    result = policy.gate(
        context,
        selection,
        evidence=evidence,
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
    policy.register_trusted_evidence(evidence)
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
    policy.register_admitted_evidence(evidence, context)
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

    assert update["decision_gate"]["permitted"] is True
    assert update["trading_decision"]["rating"] == PortfolioRating.BUY.value
    assert "Analysis Outcome" not in update.get("analysis_outcome", "")


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
        "decision admitted evidence unavailable"
    ]
    assert "trading_decision" not in update


@pytest.mark.unit
def test_graph_decision_gate_blocks_changed_source_fact_identity():
    evidence = _market_evidence()
    policy = create_production_decision_policy()
    policy.register_trusted_evidence(evidence)
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
    built = policy.build_context(
        changed_evidence,
        policy.candidate_applications(
            changed_evidence,
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
        "evidence_state": changed_evidence.model_dump(mode="json"),
    })

    assert update["decision_gate"]["permitted"] is False
    assert update["decision_gate"]["diagnostics"] == [
        "decision admitted evidence unavailable"
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

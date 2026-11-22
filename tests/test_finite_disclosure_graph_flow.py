"""Actual finite tool/submission/graph flow with scripted models and synthetic market/news."""

import json
from datetime import date, timedelta
from hashlib import sha256
from types import SimpleNamespace

import pytest
from langchain_core.messages import AIMessage

pytest_plugins = ["test_finite_disclosure_admission"]


def synthetic_market():
    from tradingagents.evidence import (
        EvidenceState,
        IdentityProvenance,
        InstrumentIdentityEvidence,
        MarketSnapshotEvidence,
        SourceArtifact,
        stable_market_snapshot_id,
    )
    from tradingagents.strategy_registry import build_market_return_fact

    days = [date(2026, 9, 26) + timedelta(days=i) for i in range(21)]
    text = "Date,Open,High,Low,Close,Volume\n" + "".join(
        f"{d},100,110,100,{110 if i == 20 else 100},100\n" for i, d in enumerate(days)
    )
    digest = sha256(text.encode()).hexdigest()
    artifact = SourceArtifact(
        artifact_sha256=digest,
        source_ref="acq.v1:market:synthetic",
        tool_call_id="synthetic-price",
        tool_name="authoritative_market_snapshot_normalized_frame_v1",
        raw_text=text,
    )
    sid = stable_market_snapshot_id(
        symbol="601328.SH",
        provider="synthetic",
        adjustment_basis="qfq",
        requested_date="2026-10-16",
        effective_trading_date="2026-10-16",
        frame_sha256=digest,
        history_rows=21,
    )
    snap = SimpleNamespace(
        symbol="601328.SH",
        adjustment_basis="qfq",
        effective_trading_date="2026-10-16",
        snapshot_id=sid,
    )
    fact = build_market_return_fact(snap, artifact)
    return EvidenceState(
        instrument_identity=InstrumentIdentityEvidence(
            symbol="601328.SH",
            venue="XSHG",
            instrument_kind="equity",
            currency="CNY",
            provenance=IdentityProvenance(
                provider="synthetic",
                source_ref="synthetic:identity",
                retrieved_at="2026-10-16T12:00:00Z",
                artifact_sha256="a" * 64,
            ),
        ),
        market_snapshot=MarketSnapshotEvidence(
            symbol="601328.SH",
            provider="synthetic",
            retrieved_at="2026-10-16T12:00:00Z",
            adjustment_basis="qfq",
            requested_date="2026-10-16",
            effective_trading_date="2026-10-16",
            history_rows=21,
            frame_sha256=digest,
            snapshot_id=sid,
        ),
        source_facts=(fact,),
        source_artifacts=(artifact,),
    )


class ScriptedModel:
    """No SDK/client constructor. Each invoke emits a nonempty closed selection."""

    def __init__(self, state_getter):
        self.state_getter = state_getter
        self.calls = 0
        self.financial_requested = False

    def selection(self):
        from tradingagents.finite_disclosure_runtime import require_context

        catalog = require_context(self.state_getter()).selection_catalog("fundamentals")
        return {
            "evidence_ids": [catalog["evidence_ids"][0]],
            "statement_ids": [catalog["statement_ids"][0]],
            "relationship": "cites",
        }

    def with_required_structured_output(self, schema):
        return SimpleNamespace(invoke=lambda prompt: self.selection())

    def with_structured_output(self, schema, **kwargs):
        return self.with_required_structured_output(schema)

    def bind_tools(self, tools, **kwargs):
        names = {getattr(t, "name", getattr(t, "__name__", "")) for t in tools}

        def invoke(prompt):
            self.calls += 1
            if "get_fundamentals" in names and not self.financial_requested:
                self.financial_requested = True
                return AIMessage(
                    content="discarded untrusted model prose",
                    tool_calls=[
                        {
                            "name": "get_fundamentals",
                            "args": {
                                "ticker": self.state_getter()["company_of_interest"],
                                "curr_date": "2026-10-16",
                            },
                            "id": "finite-positive",
                        }
                    ],
                )
            return AIMessage(
                content="discarded untrusted model prose",
                tool_calls=[
                    {
                        "name": "FiniteAdvisorySelection",
                        "args": self.selection(),
                        "id": f"selection-{self.calls}",
                    }
                ],
            )

        return SimpleNamespace(invoke=invoke)


@pytest.mark.parametrize("symbol", ["601328.SH", "601328.SS"])
def test_actual_four_analysts_admission_downstream_terminal(installation, monkeypatch, symbol):
    from tradingagents.agents.analysts import sentiment_analyst as sentiment
    from tradingagents.dataflows import config as config_module
    from tradingagents.evidence import SourceArtifact
    from tradingagents.graph.conditional_logic import ConditionalLogic
    from tradingagents.graph.financial_tools import FinancialDispatchToolNode
    from tradingagents.graph.propagation import Propagator
    from tradingagents.graph.setup import GraphSetup
    from tradingagents.strategy_registry import (
        DEFAULT_DECISION_HORIZON,
        create_production_decision_policy,
    )

    config, view, _ = installation
    monkeypatch.setattr(config_module, "get_config", lambda: config)
    monkeypatch.setattr(sentiment, "get_config", lambda: config)
    text = "Synthetic public news fixture; no transport was invoked."
    artifact = SourceArtifact(
        artifact_sha256=sha256(text.encode()).hexdigest(),
        source_ref="synthetic:news",
        tool_call_id="synthetic-inline",
        tool_name="synthetic-news",
        raw_text=text,
    )
    monkeypatch.setattr(
        sentiment,
        "acquire_news",
        lambda *args, **kwargs: SimpleNamespace(value=text, artifact=artifact, outcomes=()),
    )
    monkeypatch.setattr(
        sentiment, "get_china_a_local_sentiment", lambda *args: "Synthetic local sentiment fixture."
    )
    monkeypatch.setattr(sentiment, "get_china_a_enhancements_for_categories", lambda *args: "")
    state = Propagator().create_initial_state(
        symbol, "2026-10-16", evidence_state=synthetic_market(), run_id="run:" + "1" * 64
    )
    model = ScriptedModel(lambda: state)

    def denied_tool(s):
        raise AssertionError("unexpected nonfinancial tool request in scripted fixture")

    nodes = dict.fromkeys(("market", "social", "news"), denied_tool)
    nodes["fundamentals"] = FinancialDispatchToolNode(config=config)
    setup = GraphSetup(
        model,
        model,
        nodes,
        ConditionalLogic(max_debate_rounds=1, max_risk_discuss_rounds=1),
        decision_policy=create_production_decision_policy(),
        decision_horizon=DEFAULT_DECISION_HORIZON,
    )
    graph = setup.setup_graph().compile()
    visited = []
    for chunk in graph.stream(state, stream_mode="updates", config={"recursion_limit": 80}):
        for node, update in chunk.items():
            visited.append(node)
            if update:
                state.update({k: v for k, v in update.items() if k != "messages"})
                if update.get("messages"):
                    # Graph owns its real message reducer; this shadow is only for scripted ID selection.
                    state["messages"] = update["messages"]
    from tradingagents.dataflows.finite_disclosure_output import (
        finite_export_documents,
        validate_publication,
    )

    assert validate_publication(state, config, terminal=True)
    import copy

    ledger = state["evidence_state"]["finite_cache_reads"]
    assert validate_publication(
        {**state, "run_telemetry": {"finite_cache_reads": ledger}}, config, terminal=True
    )
    changed = copy.deepcopy(ledger)
    changed[0]["release_sha256"] = "0" * 64
    with pytest.raises(ValueError):
        validate_publication(
            {**state, "run_telemetry": {"finite_cache_reads": changed}}, config, terminal=True
        )
    with pytest.raises(ValueError):
        validate_publication(
            {**state, "run_telemetry": {"finite_cache_reads": []}}, config, terminal=True
        )
    documents, stages = finite_export_documents(state, config=config)
    assert len(stages) >= 12 and documents["complete_report.md"]
    assert "finite_publication.json" in documents
    assert state["admission_gate"]["admitted"] is True
    assert {
        "Market Analyst",
        "Sentiment Analyst",
        "News Analyst",
        "Fundamentals Analyst",
        "Bull Researcher",
        "Bear Researcher",
        "Research Manager",
        "Trader",
        "Aggressive Analyst",
        "Conservative Analyst",
        "Neutral Analyst",
        "Portfolio Manager",
        "Decision Gate",
    } <= set(visited)
    assert len(state["evidence_state"]["finite_cache_reads"]) == 1
    assert state["evidence_state"]["physical_attempt_count"] == 0
    assert all(
        state[k]
        for k in ("market_report", "sentiment_report", "news_report", "fundamentals_report")
    )
    assert state["investment_plan"] and state["trader_investment_plan"]
    assert "discarded untrusted model prose" not in json.dumps(
        {k: v for k, v in state.items() if k != "messages"}, default=str
    )


def test_cache_envelope_mutation_and_zero_request_accounting(installation):
    from tradingagents.analysis_budget import AnalysisBudget
    from tradingagents.dataflows.finite_disclosure_tools import cache_envelope, route_finite_tool
    from tradingagents.finite_disclosure_runtime import validate_finite_envelope

    config, _, _ = installation
    text = route_finite_tool(config, "get_fundamentals", "601328.SH", "2026-10-16")
    envelope = cache_envelope(config, "get_fundamentals", "test-binding", text)
    valid = validate_finite_envelope(envelope)
    budget = AnalysisBudget(30, 600)
    budget.record_finite_cache_read(valid)
    assert budget.receipt()["admitted_attempts"] == 0
    assert budget.receipt()["finite_cache_reads"][0]["new_physical_requests"] == 0
    for key, value in [
        ("release_sha256", "f" * 64),
        ("new_physical_requests", 1),
        ("full_announcement_window_coverage", True),
        ("view_sha256", "0" * 64),
    ]:
        with pytest.raises(ValueError):
            validate_finite_envelope({**envelope, key: value})


def test_fabricated_canonical_fact_cannot_enter_template(installation, monkeypatch):
    from tradingagents.dataflows import config as config_module
    from tradingagents.finite_disclosure_runtime import context_binding, require_context

    config, _, _ = installation
    monkeypatch.setattr(config_module, "get_config", lambda: config)
    original = synthetic_market()
    state = {
        "company_of_interest": "601328.SH",
        "trade_date": "2026-10-16",
        "finite_disclosure_context": context_binding(config),
        "evidence_state": original.model_dump(mode="json"),
    }
    assert require_context(state).selection_catalog("market")
    for field, value in [
        ("artifact_sha256", "f" * 64),
        ("normalized_value", "999"),
        ("fact_id", "fabricated"),
    ]:
        changed = original.model_dump(mode="json")
        changed["source_facts"][0][field] = value
        with pytest.raises(ValueError):
            require_context({**state, "evidence_state": changed}).selection_catalog("market")

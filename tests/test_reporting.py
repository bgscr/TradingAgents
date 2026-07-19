"""Report parity: the shared writer produces the report tree for the CLI and the
programmatic API alike (#1037)."""

import json
from hashlib import sha256
from types import SimpleNamespace

import pytest
from langchain_core.messages import ToolMessage

from tradingagents.decision_audit import build_decision_audit
from tradingagents.evidence import MaterialClaim, build_tool_evidence_state
from tradingagents.graph.trading_graph import TradingAgentsGraph
from tradingagents.reporting import write_report_tree


def _state():
    return {
        "market_report": "MKT",
        "news_report": "NEWS",
        "investment_debate_state": {
            "bull_history": "BULL FULL HISTORY",
            "bear_history": "BEAR FULL HISTORY",
            "judge_decision": "RM PLAN",
        },
        "trader_investment_plan": "TRADE",
        "risk_debate_state": {
            "aggressive_history": "AGGRESSIVE FULL HISTORY",
            "conservative_history": "CONSERVATIVE FULL HISTORY",
            "neutral_history": "NEUTRAL FULL HISTORY",
            "judge_decision": "PM DECISION",
        },
    }


@pytest.mark.unit
def test_write_report_tree_creates_files(tmp_path):
    out = write_report_tree(_state(), "AAPL", tmp_path)
    assert out.name == "complete_report.md"
    assert (tmp_path / "1_analysts" / "market.md").read_text() == "MKT"
    assert (tmp_path / "1_analysts" / "news.md").read_text() == "NEWS"
    assert (tmp_path / "2_research" / "manager.md").read_text() == "RM PLAN"
    assert (tmp_path / "3_trading" / "trader.md").read_text() == "TRADE"
    assert (tmp_path / "5_portfolio" / "decision.md").read_text() == "PM DECISION"
    complete = out.read_text()
    assert "Trading Analysis Report: AAPL" in complete
    assert "MKT" in complete and "PM DECISION" in complete


@pytest.mark.unit
def test_complete_report_is_summary_first_and_links_full_histories(tmp_path):
    out = write_report_tree(_state(), "AAPL", tmp_path)
    complete = out.read_text()

    assert complete.index("## I. Portfolio Manager Decision") < complete.index(
        "## II. Trading Team Plan"
    )
    assert complete.index("## II. Trading Team Plan") < complete.index(
        "## III. Research Manager Decision"
    )
    assert complete.index("## III. Research Manager Decision") < complete.index(
        "## IV. Analyst Team Reports"
    )

    assert "PM DECISION" in complete
    assert "TRADE" in complete
    assert "RM PLAN" in complete
    assert "MKT" in complete
    assert "BULL FULL HISTORY" not in complete
    assert "BEAR FULL HISTORY" not in complete
    assert "AGGRESSIVE FULL HISTORY" not in complete
    assert "CONSERVATIVE FULL HISTORY" not in complete
    assert "NEUTRAL FULL HISTORY" not in complete

    assert "[2_research/bull.md](2_research/bull.md)" in complete
    assert "[2_research/bear.md](2_research/bear.md)" in complete
    assert "[4_risk/aggressive.md](4_risk/aggressive.md)" in complete
    assert "[4_risk/conservative.md](4_risk/conservative.md)" in complete
    assert "[4_risk/neutral.md](4_risk/neutral.md)" in complete


@pytest.mark.unit
def test_blocked_analysis_writes_non_directional_outcome_report(tmp_path):
    outcome = (
        "**Analysis Outcome:** Insufficient Evidence\n"
        "**Evidence Coverage:** 50.0%\n\n"
        "Analysis stopped before thesis synthesis.\n\n"
        "No Trading Decision was issued.\n\n"
        "### Diagnostics\n\n"
        "- Required evidence missing: Authoritative Market Snapshot."
    )

    stale_directional_files = (
        tmp_path / "investment_plan.md",
        tmp_path / "trader_investment_plan.md",
        tmp_path / "final_trade_decision.md",
    )
    for path in stale_directional_files:
        path.write_text("STALE DIRECTIONAL DRAFT", encoding="utf-8")

    out = write_report_tree(
        {
            "market_report": (
                "## Market Analysis\n\n**Rating:** Buy\n\n"
                "**Price Target:** 225.0\n\n**Position Sizing:** 5%"
            ),
            "analysis_outcome": outcome,
            "investment_debate_state": {
                "bull_history": "BULL DIRECTIONAL DRAFT",
                "bear_history": "BEAR DIRECTIONAL DRAFT",
                "judge_decision": "**Rating**: Buy",
            },
            "trader_investment_plan": (
                "**Action**: Buy\n\n**Entry Price**: 190.0\n\n"
                "**Stop Loss**: 180.0\n\n**Position Sizing**: 5%\n\n"
                "FINAL TRANSACTION PROPOSAL: **BUY**"
            ),
            "risk_debate_state": {
                "aggressive_history": "AGGRESSIVE DIRECTIONAL DRAFT",
                "conservative_history": "CONSERVATIVE DIRECTIONAL DRAFT",
                "neutral_history": "NEUTRAL DIRECTIONAL DRAFT",
                "judge_decision": "**Rating**: Buy\n\n**Price Target**: 225.0",
            },
        },
        "AAPL",
        tmp_path,
    )

    assert (tmp_path / "5_portfolio" / "analysis_outcome.md").read_text() == outcome
    assert not (tmp_path / "5_portfolio" / "decision.md").exists()
    assert not (tmp_path / "2_research").exists()
    assert not (tmp_path / "3_trading").exists()
    assert not (tmp_path / "4_risk").exists()
    assert not (tmp_path / "1_analysts").exists()
    assert all(not path.exists() for path in stale_directional_files)
    complete = out.read_text()
    assert "## I. Analysis Outcome" in complete
    assert outcome in complete
    assert "Portfolio Manager Decision" not in complete
    for directional_field in (
        "Rating:",
        "Price Target:",
        "Action",
        "Entry Price",
        "Stop Loss",
        "Position Sizing",
        "FINAL TRANSACTION PROPOSAL",
    ):
        assert directional_field not in complete


@pytest.mark.unit
@pytest.mark.parametrize("blocked", (False, True))
def test_report_tree_writes_a_complete_immutable_decision_audit(tmp_path, blocked):
    state = _state()
    state.update({
        "company_of_interest": "AAPL",
        "trade_date": "2026-07-18",
        "evidence_gate_mode": "enforce",
        "evidence_state": {
            "market_snapshot": {
                "symbol": "AAPL", "provider": "test",
                "retrieved_at": "2026-07-18T00:00:00+00:00",
                "adjustment_basis": "adjusted",
                "requested_date": "2026-07-18",
                "effective_trading_date": "2026-07-17",
                "history_rows": 129,
                "frame_sha256": "f" * 64,
                "snapshot_id": "snapshot:test",
            },
        },
    })
    state["pm_selection_retry"] = {
        "rating": "Hold",
        "material_claim_ids": ["market.close"],
    }
    state["pm_revision_retry"] = {
        "retained_material_claim_ids": ["market.close"],
    }
    if blocked:
        state["analysis_outcome"] = (
            "**Analysis Outcome:** Insufficient Evidence\n\n"
            "No Trading Decision was issued."
        )

    write_report_tree(state, "AAPL", tmp_path)

    audit_path = tmp_path / "decision-audit.json"
    first_bytes = audit_path.read_bytes()
    audit = json.loads(first_bytes)
    assert audit["terminal"]["kind"] == (
        "analysis_outcome" if blocked else "trading_decision"
    )
    assert audit["evidence_state"]["market_snapshot"]["snapshot_id"] == "snapshot:test"
    assert audit["evidence_state"]["market_snapshot"]["frame_sha256"] == "f" * 64
    portfolio = audit["portfolio_manager"]
    assert portfolio["selection_retry"] == state["pm_selection_retry"]
    assert portfolio["revision_retry"] == state["pm_revision_retry"]
    assert set(portfolio["sha256"]) >= {"selection_retry", "revision_retry"}

    write_report_tree(state, "AAPL", tmp_path)
    assert audit_path.read_bytes() == first_bytes

    if blocked:
        state["analysis_outcome"] = "different non-directional terminal output"
    else:
        state["final_trade_decision"] = "different terminal output"
    with pytest.raises(FileExistsError, match="immutable decision audit"):
        write_report_tree(state, "AAPL", tmp_path)


@pytest.mark.unit
def test_logged_state_round_trips_all_immutable_decision_audit_inputs(tmp_path):
    state = {
        "company_of_interest": "AAPL",
        "trade_date": "2026-07-18",
        "asset_type": "stock",
        "graph_signature": "analysts=market|evidence_schema=2",
        "market_report": "MKT",
        "sentiment_report": "SENTIMENT",
        "news_report": "NEWS",
        "fundamentals_report": "FUNDAMENTALS",
        "evidence_gate_mode": "enforce",
        "evidence_state": {},
        "admission_gate": {"admitted": True},
        "pm_original_selection": {"material_claim_ids": ["market.close"]},
        "pm_selection_retry": {"material_claim_ids": ["market.close"]},
        "pm_revision": {"retained_material_claim_ids": ["market.close"]},
        "pm_revision_retry": {
            "retained_material_claim_ids": ["market.close"]
        },
        "original_draft_thesis": {"rating": "Hold"},
        "original_decision_gate": {"permitted": False},
        "revised_draft_thesis": {"rating": "Hold"},
        "revised_decision_gate": {"permitted": False},
        "draft_thesis": {"rating": "Unvalidated"},
        "decision_gate": {"permitted": False},
        "analysis_outcome": "**Analysis Outcome:** Insufficient Evidence",
    }
    expected_audit = build_decision_audit(state)
    graph = SimpleNamespace(
        config={"results_dir": str(tmp_path)},
        ticker="AAPL",
        log_states_dict={},
    )

    TradingAgentsGraph._log_state(graph, state["trade_date"], state)

    log_path = (
        tmp_path
        / "AAPL"
        / "TradingAgentsStrategy_logs"
        / "full_states_log_2026-07-18.json"
    )
    reloaded_state = json.loads(log_path.read_text(encoding="utf-8"))
    assert build_decision_audit(reloaded_state) == expected_audit


@pytest.mark.unit
def test_decision_audit_embeds_the_hashed_source_artifact():
    content = "The effective-date close was 4.31."
    source_ref = "issuer_filing:AAPL:2026-07-18"
    claim = MaterialClaim(
        claim_id="fundamentals.close",
        analyst="fundamentals",
        statement=content,
        source_quote=content,
        source_refs=(source_ref,),
    )
    evidence = build_tool_evidence_state(
        (
            ToolMessage(
                content=content,
                name="issuer_filing",
                tool_call_id="call-filing",
            ),
        ),
        (claim,),
        tool_call_ids_by_source={source_ref: ("call-filing",)},
    )

    audit = build_decision_audit(
        {
            "analysis_outcome": "Insufficient Evidence",
            "evidence_state": evidence.model_dump(mode="json"),
        }
    )

    artifact = audit["evidence_state"]["source_artifacts"][0]
    assert artifact["raw_text"] == content
    assert artifact["artifact_sha256"] == sha256(content.encode()).hexdigest()
    fact = audit["evidence_state"]["source_facts"][0]
    assert artifact["raw_text"][fact["source_span_start"] : fact["source_span_end"]] == (
        fact["raw_text"]
    )


@pytest.mark.unit
def test_save_reports_explicit_path(tmp_path):
    # Unbound: with an explicit save_path, the method doesn't touch self/config.
    out = TradingAgentsGraph.save_reports(None, _state(), "AAPL", save_path=tmp_path)
    assert (tmp_path / "complete_report.md").exists()
    assert out == tmp_path / "complete_report.md"


@pytest.mark.unit
def test_save_reports_defaults_under_results_dir(tmp_path):
    mock_self = SimpleNamespace(config={"results_dir": str(tmp_path)})
    out = TradingAgentsGraph.save_reports(mock_self, _state(), "AAPL")
    assert out.exists()
    assert out.parent.parent.name == "reports"  # results_dir/reports/AAPL_<stamp>/...
    assert out.parent.name.startswith("AAPL_")

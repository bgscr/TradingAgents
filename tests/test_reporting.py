"""Report parity: the shared writer produces the report tree for the CLI and the
programmatic API alike (#1037)."""

from types import SimpleNamespace

import pytest

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

    assert "2_research/bull.md" in complete
    assert "2_research/bear.md" in complete
    assert "4_risk/aggressive.md" in complete
    assert "4_risk/conservative.md" in complete
    assert "4_risk/neutral.md" in complete


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

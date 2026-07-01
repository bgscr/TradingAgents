"""Reusable report-tree writer shared by the CLI and the programmatic API.

Writes a run's per-section markdown (analysts, research, trading, risk,
portfolio) plus a consolidated ``complete_report.md`` under ``save_path``. The
CLI and ``TradingAgentsGraph.save_reports`` both call this, so a headless / API
run produces the same on-disk report tree a CLI run does.
"""

from datetime import datetime
from pathlib import Path


def _write_markdown(path: Path, text: str) -> None:
    path.parent.mkdir(exist_ok=True)
    path.write_text(text, encoding="utf-8")


def _appendix_entry(label: str, path: Path, root: Path) -> str:
    return f"- {label}: `{path.relative_to(root).as_posix()}`"


def write_report_tree(final_state: dict, ticker: str, save_path) -> Path:
    """Save a completed run's reports to ``save_path``; return the complete-report path."""
    save_path = Path(save_path)
    save_path.mkdir(parents=True, exist_ok=True)
    complete_sections = []
    appendix_entries = []

    analysts_dir = save_path / "1_analysts"
    analyst_parts = []
    if final_state.get("market_report"):
        path = analysts_dir / "market.md"
        _write_markdown(path, final_state["market_report"])
        analyst_parts.append(("Market Analyst", final_state["market_report"]))
    if final_state.get("sentiment_report"):
        path = analysts_dir / "sentiment.md"
        _write_markdown(path, final_state["sentiment_report"])
        analyst_parts.append(("Sentiment Analyst", final_state["sentiment_report"]))
    if final_state.get("news_report"):
        path = analysts_dir / "news.md"
        _write_markdown(path, final_state["news_report"])
        analyst_parts.append(("News Analyst", final_state["news_report"]))
    if final_state.get("fundamentals_report"):
        path = analysts_dir / "fundamentals.md"
        _write_markdown(path, final_state["fundamentals_report"])
        analyst_parts.append(("Fundamentals Analyst", final_state["fundamentals_report"]))

    research_manager = None
    if final_state.get("investment_debate_state"):
        research_dir = save_path / "2_research"
        debate = final_state["investment_debate_state"]
        if debate.get("bull_history"):
            path = research_dir / "bull.md"
            _write_markdown(path, debate["bull_history"])
            appendix_entries.append(_appendix_entry("Bull researcher full history", path, save_path))
        if debate.get("bear_history"):
            path = research_dir / "bear.md"
            _write_markdown(path, debate["bear_history"])
            appendix_entries.append(_appendix_entry("Bear researcher full history", path, save_path))
        if debate.get("judge_decision"):
            path = research_dir / "manager.md"
            _write_markdown(path, debate["judge_decision"])
            research_manager = debate["judge_decision"]

    trader_plan = final_state.get("trader_investment_plan")
    if trader_plan:
        _write_markdown(save_path / "3_trading" / "trader.md", trader_plan)

    portfolio_decision = None
    if final_state.get("risk_debate_state"):
        risk_dir = save_path / "4_risk"
        risk = final_state["risk_debate_state"]
        if risk.get("aggressive_history"):
            path = risk_dir / "aggressive.md"
            _write_markdown(path, risk["aggressive_history"])
            appendix_entries.append(_appendix_entry("Aggressive analyst full history", path, save_path))
        if risk.get("conservative_history"):
            path = risk_dir / "conservative.md"
            _write_markdown(path, risk["conservative_history"])
            appendix_entries.append(_appendix_entry("Conservative analyst full history", path, save_path))
        if risk.get("neutral_history"):
            path = risk_dir / "neutral.md"
            _write_markdown(path, risk["neutral_history"])
            appendix_entries.append(_appendix_entry("Neutral analyst full history", path, save_path))
        if risk.get("judge_decision"):
            portfolio_decision = risk["judge_decision"]
            _write_markdown(save_path / "5_portfolio" / "decision.md", portfolio_decision)

    if portfolio_decision:
        complete_sections.append(
            f"## I. Portfolio Manager Decision\n\n### Portfolio Manager\n{portfolio_decision}"
        )
    if trader_plan:
        complete_sections.append(f"## II. Trading Team Plan\n\n### Trader\n{trader_plan}")
    if research_manager:
        complete_sections.append(
            f"## III. Research Manager Decision\n\n### Research Manager\n{research_manager}"
        )
    if analyst_parts:
        content = "\n\n".join(f"### {name}\n{text}" for name, text in analyst_parts)
        complete_sections.append(f"## IV. Analyst Team Reports\n\n{content}")
    if appendix_entries:
        complete_sections.append("## Appendix: Full Debate Files\n\n" + "\n".join(appendix_entries))

    # Write consolidated report
    header = f"# Trading Analysis Report: {ticker}\n\nGenerated: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n\n"
    (save_path / "complete_report.md").write_text(
        header + "\n\n".join(complete_sections),
        encoding="utf-8",
    )
    return save_path / "complete_report.md"

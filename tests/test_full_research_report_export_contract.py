"""Exercise the CLI export seam with a synthetic sealed research terminal."""
from hashlib import sha256
from unittest.mock import Mock

import pytest


@pytest.mark.parametrize("opinion", [True, False])
def test_chinese_research_export_without_optional_sources(tmp_path, monkeypatch, opinion):
    from cli.main import save_report_to_disk
    from tradingagents import portfolio_research_terminal as terminal

    symbol = "000001.SZ"
    item = dict(version="portfolio-research-v3", run="1" * 32, graph="2" * 32,
                symbol=symbol, as_of="2026-11-25", selection="3" * 64,
                status="research_opinion" if opinion else "no_conclusion")
    if opinion:
        item["submission"] = terminal.Submission(
            rating="Hold", support="bull", opposition="bear",
            stages=["bull", "bear", "manager", "trader", "risk"],
            market_fact="fixture-market", financial_fact="fixture-finance",
            horizon="中期", tradeoff="合成研究意见").model_dump(mode="json")
    checksum = sha256(terminal.canonical(item)).hexdigest()
    sources = {"financial": {"observations": {"fixture-finance": {
        "symbol": symbol, "field": "净利润", "value": "19", "unit": "元",
        "period_end": "2026-09-30", "period_basis": "报告期累计"}}},
        "market": {"observations": {"fixture-market": {"normalized_value": "0.02"}}}}
    ledger = Mock()
    monkeypatch.setattr(terminal, "read_terminal", lambda state: (item, checksum))
    monkeypatch.setattr(terminal, "read_current", sources.__getitem__)
    monkeypatch.setattr(terminal, "active_selection", lambda: (ledger, None))
    report = save_report_to_disk({"portfolio_research_terminal": checksum}, symbol,
                                 tmp_path, output_language="Chinese")
    body = report.read_text(encoding="utf-8")
    assert symbol in body
    if opinion:
        assert "组合经理综合研究评级：Hold" in body
        assert "已核验财务观测" in body and "净利润" in body and "19 元" in body
    else:
        assert "无可用结论" in body and "净利润" not in body
    key, raw, digest, origin, cost = ledger.register.call_args.args
    assert key.startswith("research-export/") and sha256(raw).hexdigest() == digest
    assert origin == "portfolio-research-v3" and cost == 0

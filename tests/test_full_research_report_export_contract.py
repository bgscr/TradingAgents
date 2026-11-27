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


@pytest.fixture
def sealed_research_export(tmp_path, monkeypatch):
    from types import SimpleNamespace

    from tradingagents import portfolio_research_terminal as terminal

    run = SimpleNamespace(run_id="1" * 32, selection_sha256="3" * 64,
                          symbol="000001.SZ", as_of="2026-11-25")
    graph = "2" * 32
    state = {"run_id": graph, "company_of_interest": run.symbol, "trade_date": run.as_of}
    ledger = Mock()
    ledger.folder = tmp_path / "run" / "ledger"
    evidence = ledger.folder.parent / "research-evidence"
    evidence.mkdir(parents=True)
    records = {}

    def seal(key, value):
        raw = terminal.canonical(value)
        digest = sha256(raw).hexdigest()
        (evidence / (digest + ".json")).write_bytes(raw)
        records[key] = (key, digest, "portfolio-research-v3", 0)
        return digest

    ledger.snapshot.side_effect = lambda: {"evidence": list(records.values())}
    monkeypatch.setattr(terminal, "active_selection", lambda: (ledger, run))
    seal("research-graph-identity", {"owner": run.run_id, "graph": graph,
                                     "selection": run.selection_sha256})
    stages = ["bull", "bear", "manager", "trader", "risk"]
    for stage in stages:
        seal(stage, {"stage": stage, "graph": graph})
    submission = terminal.Submission(
        rating="Hold", support="bull", opposition="bear", stages=stages,
        market_fact="fixture-market", financial_fact="fixture-finance",
        horizon="中期", tradeoff="合成研究意见")
    sources = {kind: {"payload_sha256": sha256(terminal.canonical(
        {"source": kind, "observations": {}})).hexdigest()} for kind in ("market", "financial")}
    monkeypatch.setattr(terminal, "read_current", sources.__getitem__)
    item = {"version": "portfolio-research-v3", "run": run.run_id, "graph": graph,
            "symbol": run.symbol, "as_of": run.as_of, "selection": run.selection_sha256,
            "status": "research_opinion", "submission": submission.model_dump(mode="json"),
            "market": sources["market"]["payload_sha256"],
            "financial": sources["financial"]["payload_sha256"]}

    def seal_terminal():
        state["portfolio_research_terminal"] = seal("research-terminal/" + graph, item)

    seal_terminal()
    assert terminal.read_terminal(state) == (item, state["portfolio_research_terminal"])
    return state, item, sources, seal_terminal, ledger


@pytest.mark.parametrize("field,foreign_id", [("run", "4" * 32), ("selection", "5" * 64)])
def test_export_rejects_terminal_from_another_run_or_selection(
        tmp_path, sealed_research_export, field, foreign_id):
    from cli.main import save_report_to_disk

    state, item, _, seal_terminal, ledger = sealed_research_export
    item[field] = foreign_id
    seal_terminal()
    target = tmp_path / "unpublished"
    with pytest.raises(ValueError, match="^research_terminal_mismatch$"):
        save_report_to_disk(state, item["symbol"], target, output_language="Chinese")
    assert not target.exists()
    ledger.register.assert_not_called()


def test_export_rejects_changed_financial_source(tmp_path, sealed_research_export):
    from cli.main import save_report_to_disk

    state, item, sources, _, ledger = sealed_research_export
    sources["financial"]["payload_sha256"] = sha256(b"revised synthetic observation").hexdigest()
    target = tmp_path / "unpublished"
    with pytest.raises(ValueError, match="^research_terminal_source_changed$"):
        save_report_to_disk(state, item["symbol"], target, output_language="Chinese")
    assert not target.exists()
    ledger.register.assert_not_called()

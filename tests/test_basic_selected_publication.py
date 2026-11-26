"""Both Basic collectors share the session budget and an on-disk publication barrier."""
import hashlib
import json
from concurrent.futures import ThreadPoolExecutor
from threading import Event
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from tests.test_bounded_mainland_entry import entry
from tradingagents import analysis_budget, margin_balance, powershell_fundflow, research_stream


@pytest.mark.parametrize("outcome,limit,expected", [
    ("report_error", 2, []), ("receipt_error", 2, []),
    ("success", 2, ["fundflow", "margin"]), ("success", 1, ["fundflow"]),
])
def test_selected_basic_sources_require_publication(tmp_path, monkeypatch, outcome, limit, expected):
    module = entry()
    symbol, day = "601328.SS", "2026-11-20"
    session = module.Session(tmp_path, source=tmp_path, margin_date=day,
                             powershell_fundflow_executable=tmp_path / "fixture.exe")
    session.selection = {"ticker": symbol, "analysis_date": day}
    results = session.paths["results_dir"]
    export_id = "1" * 32
    report = results / symbol / day / "runs/20261120_120000/reports/report_exports" / export_id / "complete_report.md"
    budget = analysis_budget.AnalysisBudget(limit, 60)
    sends = []
    published = Event()

    class SynchronizedWorker(ThreadPoolExecutor):
        def submit(self, *args, **kwargs):
            future = super().submit(*args, **kwargs)
            future.result(timeout=4)
            return future

    monkeypatch.setattr("concurrent.futures.ThreadPoolExecutor", SynchronizedWorker)

    def send(kind):
        if budget.admit("data", optional=True) is not None:
            sends.append((kind, published.is_set(), module.verified_basic(tmp_path, results) is not None))

    flow = Mock()
    flow.collect_after_report.side_effect = lambda: send("fundflow")
    flow.summary.return_value = {"observation": {}, "transport": {}}
    margin = Mock(result=None, transport={})

    def collect_margin():
        send("margin")
        return {"status": "unavailable"}

    margin.prepare.return_value = collect_margin
    monkeypatch.setattr(powershell_fundflow, "PowerShellFundflow", lambda *args, **kwargs: flow)
    monkeypatch.setattr(margin_balance, "_Run", lambda *args: margin)
    monkeypatch.setattr(research_stream, "full_graph_valid", lambda state: True)

    def publish(*args):
        if outcome == "report_error":
            raise OSError("synthetic report error")
        report.parent.mkdir(parents=True)
        report.write_bytes(b"synthetic basic report")
        digest = hashlib.sha256(report.read_bytes()).hexdigest()
        (report.parent / "manifest.json").write_text(json.dumps({
            "export_id": export_id, "files": {"complete_report.md": digest}
        }), encoding="utf8")
        return report

    original_write = module.write_once

    def write(root, name, value):
        if name == "basic-publication.json" and outcome == "receipt_error":
            raise OSError("synthetic receipt error")
        original_write(root, name, value)
        if name == "basic-publication.json":
            assert module.verified_basic(tmp_path, results) == value
            published.set()

    monkeypatch.setattr(module, "write_once", write)
    cli = SimpleNamespace(DEFAULT_CONFIG={}, get_user_selections=Mock(), _build_run_config=Mock(),
                          _prepare_run_artifacts=Mock(), _mark_run_failed=Mock(), _write_run_reports=publish)
    with session.analysis_hooks(cli, {}, {}):
        if outcome.endswith("error"):
            with pytest.raises(OSError, match="synthetic"):
                cli._write_run_reports({}, symbol, {})
        else:
            assert cli._write_run_reports({}, symbol, {}) == report
    assert sends == [(kind, True, True) for kind in expected]
    assert budget.receipt()["data_attempts"] == len(expected)

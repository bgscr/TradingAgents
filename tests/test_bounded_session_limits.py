"""Synthetic sends and disposable sleeping processes; no application credentials."""
import os
import json
from pathlib import Path
import subprocess
import sys
import sqlite3
import threading
import time
from types import SimpleNamespace

import pytest

from tests.test_bounded_mainland_entry import entry


@pytest.mark.parametrize("identity,calendar,finance", [(0, 0, 0), (0, 1, 3), (1, 1, 3)])
def test_session_remainder_charges_each_wire_attempt(identity, calendar, finance):
    from tradingagents.analysis_budget import AnalysisBudget, AnalysisBudgetStopped

    m = entry()
    limit = m.analysis_allowance(identity, calendar, finance)
    budget = AnalysisBudget(limit, 1200)
    sent = ["identity"] * identity + ["calendar"] * calendar + ["financial"] * finance
    # Mixed data and model admissions use the same remaining counter.
    for kind in (["data", "data", "data", "model", "model"] * 12)[:limit]:
        budget.admit(kind)
        sent.append(kind)
    assert len(sent) == 60
    with pytest.raises(AnalysisBudgetStopped):
        budget.admit("model")


def test_four_financial_attempts_cannot_enter_graph():
    m = entry()
    with pytest.raises(m.Stop):
        m.analysis_allowance(0, 1, 4)


def test_fourth_financial_query_stops_before_wire(tmp_path, monkeypatch):
    from tests.test_financial_acceptance_scope import fake_https, sample_rows
    from tradingagents import statement_acceptance as finance
    from tradingagents.dataflows.provider_subrequests import PhysicalAttemptFailure

    sends = fake_https(monkeypatch, sample_rows)
    def four_queries(*args, **kwargs):
        client = finance.current_tushare_statements.create_financial_statement_client(None)
        for endpoint in ("income", "balancesheet", "cashflow"):
            client.query(endpoint, ts_code="601328.SH", start_date="20260101",
                         end_date="20261113", report_type="1")
        with pytest.raises(PhysicalAttemptFailure):
            client.query("income", ts_code="601328.SH", start_date="20260101",
                         end_date="20261113", report_type="1")
        return {}
    monkeypatch.setattr(finance, "_run", four_queries)
    result = finance.acquire(tmp_path, ticker="601328.SH", destination="https://api.tushare.pro",
                             account="synthetic", as_of="2026-11-13", token="synthetic")
    assert len(sends) == result["physical_requests"] == 3


@pytest.mark.parametrize("financial", [0, 3])
def test_execute_rebuilds_graph_config_with_verified_remainder(tmp_path, monkeypatch, financial):
    from tradingagents.analysis_budget import AnalysisBudget, AnalysisBudgetStopped

    m = entry()
    s = m.Session(m.reserve_session(tmp_path / "runs"), source=tmp_path)
    (tmp_path / "config").mkdir()
    (tmp_path / "config/instrument_identity_registry.sha256").write_text("a" * 64)
    s.clock = m.SharedSessionClock()
    s.identity_calls = 1
    s.calendar_handoff = {"sha256": "b" * 64}
    monkeypatch.setattr(s, "check_calendar_handoff", lambda config=None: 1)
    monkeypatch.setattr(s, "precheck", lambda config: s.configure(config))
    monkeypatch.setattr(s, "refresh", lambda: [financial, 0])
    cli = SimpleNamespace(DEFAULT_CONFIG={}, get_user_selections=lambda: {},
                          _build_run_config=lambda: {}, _prepare_run_artifacts=lambda: {})
    def graph(cli, config):
        with s.analysis_hooks(cli, config, {}):
            rebuilt = cli._build_run_config()
            assert rebuilt["analysis_max_calls"] == config["analysis_max_calls"] == 58 - financial
            assert rebuilt["analysis_session_monotonic_deadline"] == s.clock.deadline_monotonic
            budget = AnalysisBudget(rebuilt["analysis_max_calls"], 1200,
                session_deadline_epoch=rebuilt["analysis_session_deadline_epoch"],
                session_monotonic_deadline=rebuilt["analysis_session_monotonic_deadline"])
            connection = sqlite3.connect(":memory:")
            try:
                budget.bind_checkpoint(connection, threading.Lock(), "fake-thread", "policy", has_checkpoint=False)
                for _ in range(58 - financial):
                    budget.admit("data")
                with pytest.raises(AnalysisBudgetStopped, match="call_limit_exceeded"):
                    budget.admit("model")
            finally:
                connection.close()
        return {"status": "completed"}
    monkeypatch.setattr(s, "analyze", graph)
    result = s.execute(cli, {}, verify_sources=lambda: None, requested_selection=("601328.SH", "2026-01-02"))
    assert result["status"] == "completed", result
    assert json.loads((s.root / "preparation-counts.json").read_text())["remaining_for_analysis"] == 58 - financial


def test_financial_permission_expiring_before_send():
    from contextlib import nullcontext
    from scripts import accept_financial_statements as finance
    connection = SimpleNamespace(timeout=30)
    with pytest.raises(RuntimeError, match="financial_send_deadline_exceeded"):
        with finance.bounded_exchange(nullcontext(), connection,
                {"session_deadline_epoch": 1, "session_monotonic_deadline": 1}):
            pytest.fail("expired permission reached transport")


def test_identity_expires_while_constructing_connection(monkeypatch):
    from tradingagents.dataflows import bank_identity_acquisition as identity
    tick = [0.]
    clock = identity.CaptureClock(clock=lambda: tick[0], wall=lambda: 1000.)
    clock.end = 2.
    def connection(*args, **kwargs):
        tick[0] = 2.
        return SimpleNamespace(close=lambda: None, request=lambda *a, **k: pytest.fail("late identity send"))
    monkeypatch.setattr(identity.http.client, "HTTPSConnection", connection)
    receipt = {"physical_attempts": 0}
    with pytest.raises(Exception, match="bank_"):
        identity.request_partition("synthetic", "SSE", "L", clock, receipt)
    assert receipt["physical_attempts"] == 0


@pytest.mark.skipif(sys.platform != "win32", reason="Windows process ownership")
def test_supervisor_preserves_piped_io_and_worker_exit(tmp_path):
    src = Path(__file__).resolve().parents[1] / "scripts/analyze_mainland_bounded.py"
    supervisor = ("import runpy,sys,time; m=runpy.run_path(sys.argv[1]); "
                  "sys.exit(m['supervise']([sys.executable,'-I','-S','-B','-c',sys.argv[2]],"
                  "time.monotonic()+10))")
    worker = "import sys; print(input()); print('fixture',file=sys.stderr); sys.exit(7)"
    result = subprocess.run([sys.executable, "-I", "-S", "-B", "-c", supervisor, str(src), worker],
                            cwd=tmp_path, env={"SystemRoot": os.environ["SystemRoot"]},
                            input="fake input\n", capture_output=True, text=True, timeout=12)
    assert (result.returncode, result.stdout.strip(), result.stderr.strip()) == (7, "fake input", "fixture")


@pytest.mark.skipif(sys.platform != "win32", reason="Windows process ownership")
@pytest.mark.parametrize("session_started", [False, True])
def test_timeout_removes_only_owned_descendants(tmp_path, session_started):
    src = Path(__file__).resolve().parents[1] / "scripts/analyze_mainland_bounded.py"
    # An unrelated sibling holds a separate lifetime. A grandchild holds a file
    # handle that Windows refuses to unlink until the owned process tree exits.
    leaf = "import time; f=open('held','xb'); f.write(b'1'); f.flush(); time.sleep(60)"
    session = tmp_path / ("a" * 32)
    if session_started:
        session.mkdir()
        (session / "session.json").write_text(json.dumps({"contract": "bounded-mainland-session-v1",
            "run_id": session.name, "root": str(session), "resume": False}))
    worker = ("import sys, subprocess, time; subprocess.Popen([sys.executable,"
              f"'-I','-S','-B','-c',{leaf!r}]); time.sleep(60)")
    supervise = ("import runpy,sys,time; m=runpy.run_path(sys.argv[1]); "
                 "sys.exit(m['supervise']([sys.executable,'-I','-S','-B','-c',sys.argv[2]],"
                 "time.monotonic()+2,session_root=m['Path'](sys.argv[3])))")
    env = {k: os.environ[k] for k in ("SystemRoot", "TEMP", "TMP") if k in os.environ}
    flags = [sys.executable, "-I", "-S", "-B", "-c"]
    with subprocess.Popen(flags + ["import time; time.sleep(60)"], env=env, cwd=tmp_path) as sibling:
        try:
            result = subprocess.run(flags + [supervise, str(src), worker, str(session)], cwd=tmp_path,
                                    env=env, timeout=12, check=False, capture_output=True)
            assert result.returncode == 124
            receipt = json.loads(result.stdout)
            assert receipt["run_id"] == session.name and receipt["owned_process_exit_confirmed"]
            assert receipt["job_empty_confirmed"]
            if session_started:
                assert json.loads((session / "supervisor-result.json").read_text()) == receipt
            else:
                assert not session.exists()
            assert sibling.poll() is None
            held = tmp_path / "held"
            assert held.read_bytes() == b"1"
            for _ in range(100):
                try:
                    held.unlink()
                    break
                except PermissionError:
                    time.sleep(.01)
            assert not held.exists()
        finally:
            sibling.kill()

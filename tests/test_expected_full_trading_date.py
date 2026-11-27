"""Offline coverage for the ordinary Full latest-date assertion."""
import json
import os
import runpy
import shutil
import subprocess
import sys
import venv
from hashlib import sha256
from pathlib import Path
from types import SimpleNamespace

import pytest

ROOT = Path(__file__).resolve().parents[1]
INTAKE = ROOT / "scripts/full_latest_intake.py"
ARGS = ["--full", "--ticker", "601328.SH", "--analysis-date", "latest", "--run", "--use-project-env"]


def test_optional_date_is_canonical_and_bound_to_intent():
    intake = runpy.run_path(str(INTAKE))["intake"]
    old, _ = intake(ARGS)
    assert "expected_trading_date" not in old and old["requested_date"] == "latest"
    guarded, _ = intake([*ARGS, "--expected-trading-date", "2026-11-27"])
    assert guarded == {**old, "expected_trading_date": "2026-11-27"}
    for invalid in ("20261127", "2026-02-30", ""):
        with pytest.raises(ValueError):
            intake([*ARGS, "--expected-trading-date", invalid])
    for extra in (["--session-max-calls", "61"], ["--calendar-only", "--session-max-calls", "1"]):
        with pytest.raises(ValueError):
            intake([*ARGS, "--expected-trading-date", "2026-11-27", *extra])


@pytest.mark.parametrize("selected,expected,stop", [
    ("2026-11-27", "2026-11-27", None), ("2026-11-26", "2026-11-27", "unexpected_trading_date"),
    ("prior-needs-daily", "2026-11-27", "unexpected_daily_request_date"),
    (None, "2026-11-27", "security_close_missing"), ("2026-11-26", None, None),
    ("2026-11-13", "2026-11-13", "prior_dispatch_unreconciled"),
    ("bad-proof", "2026-11-27", "resolution_binding_mismatch"),
])
def test_live_intake_handoff_with_synthetic_reader(tmp_path, monkeypatch, selected, expected, stop):
    """Exercise the production intake and ledger in a temporary root. No provider implementation is used."""
    import ctypes
    import socket

    module = runpy.run_path(str(INTAKE))
    monkeypatch.syspath_prepend(str(ROOT / "scripts"))
    monkeypatch.setattr(sys, "path", list(sys.path))
    monkeypatch.setattr(ctypes, "windll", SimpleNamespace(shell32=SimpleNamespace(IsUserAnAdmin=lambda: False)),
                        raising=False)
    monkeypatch.delenv("TRADINGAGENTS_PROTECTION_CONTEXT", raising=False)
    # Model a normal invocation using an empty process environment and fixture transports.
    for key in list(os.environ):
        if key.upper() not in {"WINDIR", "SYSTEMROOT", "TEMP", "TMP", "PATH", "USERPROFILE"}:
            monkeypatch.delenv(key)
    monkeypatch.setenv("PYTHON_DOTENV_DISABLED", "1")
    clock = SimpleNamespace(remaining=lambda: 10)
    rid = "a" * 32
    root = tmp_path / "reports/bounded-runs" / rid
    root.mkdir(parents=True)
    observed = []
    class Stop(Exception):
        pass
    def require(ok, reason):
        if not ok:
            raise Stop(reason)

    def write(folder, name, data):
        with (folder / name).open("x", encoding="utf8") as f:
            json.dump(data, f)

    def no_io(*args, **kwargs):
        pytest.fail("unexpected transport")

    monkeypatch.setattr(socket.socket, "connect", no_io)
    monkeypatch.setattr(socket, "getaddrinfo", no_io)
    monkeypatch.setattr(subprocess, "Popen", no_io)
    monkeypatch.setattr(os, "system", no_io)
    # All data, credential and protocol edges are fixture-owned modules.
    from full_financial_channel import digest, write as write_record
    channel = SimpleNamespace(digest=digest, write=write_record, load=lambda path: json.loads(path.read_bytes()),
                              binding=lambda source, folder: json.loads((folder / "full-owner.json").read_bytes()))
    monkeypatch.setitem(sys.modules, "full_financial_channel", channel)
    monkeypatch.setitem(sys.modules, "full_cold_calendar", SimpleNamespace(CALENDAR_FAILURES={}, prepare=no_io))
    monkeypatch.setitem(sys.modules, "full_cold_daily", SimpleNamespace(DAILY_STOP_CODES=set(), acquire=no_io))
    monkeypatch.setitem(sys.modules, "full_cold_identity", SimpleNamespace(acquire_identity=lambda *args: None))
    monkeypatch.setitem(sys.modules, "tradingagents.dataflows.bank_identity_snapshot",
                        SimpleNamespace(find_current_reference=lambda *args, **kw: (None, "base_registry")))
    monkeypatch.setitem(sys.modules, "tradingagents.market_history.coordinator",
                        SimpleNamespace(PhysicalAttemptBudgetExhausted=AssertionError))
    # Only the calendar reader may be invoked, always with latest and this owner.
    def reader(cmd, **kw):
        assert Path(cmd[3]).name == "full_calendar_cache.py" and cmd[4:6] == ["601328.SS", "latest"]
        assert cmd[-3:] == ["-", "-", rid]
        if selected == "prior-needs-daily":
            need = {"error": "same_day_close_required", "need": {"trading_date": "2026-11-26"}}
            return subprocess.CompletedProcess(cmd, 2, json.dumps(need).encode())
        receipt = (dict(trading_date=selected, security="601328.SS", venue="XSHG", calendar_revision="fixture",
                        purpose="current_retrospective_selection", close_evidence={"trading_date": selected})
                   if selected else {"error": "security_close_missing"})
        if selected == "bad-proof":
            receipt.update(trading_date="2026-11-26", close_evidence={})
        return subprocess.CompletedProcess(cmd, 0 if selected else 1, json.dumps(receipt).encode())
    monkeypatch.setattr(subprocess, "run", reader)
    (tmp_path / "scripts").mkdir()
    (tmp_path / "scripts/full_date_evidence.py").write_text("", encoding="utf8")
    def handoff(entry, intent, day, args, root, clock, manifest, evidence):
        observed.append(day)
        assert args.argv[args.argv.index("--session-max-calls") + 1] == "60"
        assert evidence["selection_receipt"]["trading_date"] == day
        return 0
    monkeypatch.setitem(module["run"].__globals__, "export_after_verified_selection", handoff)
    recipe = json.loads((ROOT / "config/ordinary-full-release-lock.json").read_bytes())
    manifest = {"files": [{"root": "project", "relative": k} for k in recipe["project"]]}
    entry = SimpleNamespace(SOURCE=tmp_path, FULL_BOUND_FILES=(), Stop=Stop, require=require,
                            check_required_local_stores=lambda source: None, reserve_session=lambda *a, **kw: root,
                            FIXED={"tushare_account_scope_label": "fixture"}, write_once=write,
                            parse_arguments=lambda argv: SimpleNamespace(argv=argv))
    argv = ARGS if expected is None else [*ARGS, "--expected-trading-date", expected]
    if stop:
        with pytest.raises(ValueError if stop == "resolution_binding_mismatch" else Stop, match=stop):
            module["run"](entry, argv, clock, rid, manifest)
        reason = json.loads((root / "result.json").read_bytes())["reason"]
        assert reason == ("preparation_failed" if stop == "resolution_binding_mismatch" else stop) and not observed
        if stop == "unexpected_trading_date":
            bound = json.loads((root / "resolved-selection.json").read_bytes())
            assert bound["run_id"] == rid and bound["selection"]["trading_date"] == selected
            assert bound["params_sha256"] == digest(module["intake"](argv)[0])
        elif stop != "prior_dispatch_unreconciled":
            assert not (root / "resolved-selection.json").exists()
    else:
        assert module["run"](entry, argv, clock, rid, manifest) == 0 and observed == [selected]
    assert json.loads((root / "physical-total.json").read_bytes())["total_external_attempts"] == 0


@pytest.mark.skipif(sys.platform != "win32", reason="Windows PowerShell launcher")
def test_public_launcher_passes_assertion_without_sending(tmp_path):
    host = shutil.which("pwsh.exe")
    if not host:
        pytest.skip("reviewed PowerShell 7 required")
    policy = json.loads((ROOT / "config/ordinary-full-pair-policy.json").read_bytes())
    if sha256(Path(host).read_bytes()).hexdigest() != policy["executable_sha256"]:
        pytest.skip("host differs from reviewed binary")
    (tmp_path / "scripts").mkdir()
    for name in ("start_tradingagents.ps1", "scripts/Start-MainlandAnalysis.ps1"):
        shutil.copyfile(ROOT / name, tmp_path / name)
    venv.EnvBuilder(with_pip=False).create(tmp_path / ".venv")
    (tmp_path / "scripts/analyze_mainland_bounded.py").write_text("import json,sys\nprint(json.dumps(sys.argv[1:]))\n")
    cmd = [host, "-NoProfile", "-NonInteractive", "-File", str(tmp_path / "start_tradingagents.ps1"),
           "-Full", "-Ticker", "601328.SH", "-ExpectedTradingDate", "2026-11-27"]
    env = {k: v for k, v in os.environ.items() if k.upper() in {"WINDIR", "SYSTEMROOT"}}
    for flags, pyflag in ((["-Run", "-UseProjectEnv"], "--run"), (["-CheckOnly"], "--check-only")):
        p = subprocess.run(cmd + flags, cwd=tmp_path, env=env, check=True, capture_output=True, timeout=20)
        args = json.loads(p.stdout)
        assert pyflag in args and "--auto-latest" in args
        assert args[args.index("--expected-trading-date") + 1] == "2026-11-27"
        assert args[args.index("--analysis-date") + 1] == "latest"
        assert args[args.index("--session-max-calls") + 1] == "60"

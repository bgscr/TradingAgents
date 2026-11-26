"""Exercise the public Full launcher with a no-send child at the provider boundary."""
import os
from pathlib import Path
import shutil
import subprocess
import sys
import json

import pytest

PROBE = r"""
import sys
import json
import os
from pathlib import Path
def offline(event, args):
    if event.startswith(("socket.", "subprocess.")) or event == "os.system":
        raise RuntimeError("transport forbidden")
sys.addaudithook(offline)
root = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(root))
from tradingagents.full_pair_producer import load_policy, ProducedSource
from tradingagents.full_factory import session_sources
from tradingagents.full_matrix import FLOW, margin_source
from tradingagents.full_publication import full_sources
manifest = json.loads((root / "config/bounded_mainland_release.json").read_bytes())
pins = {row["relative"]: row["sha256"] for row in manifest["files"] if row["root"] == "project"}
runtime = load_policy(root, pins)
assert runtime and "--check-only" in sys.argv and "--run" not in sys.argv
assert sys.argv[sys.argv.index("--session-max-calls") + 1] == "60"
selection = {"ticker": "601328.SS", "analysis_date": "2026-11-20"}
names = {FLOW, margin_source(selection["ticker"])}
with session_sources(selection, None, selected=names, producer_config={}, pair_runtime=runtime):
    sources = full_sources.get()
    assert set(sources) == names and all(isinstance(s, ProducedSource) for s in sources.values())
print(json.dumps({"registered": len(names), "locator": os.environ["TRADINGAGENTS_ORDINARY_FULL_PWSH"]}))
"""


@pytest.mark.skipif(sys.platform != "win32", reason="Windows public Full launcher")
def test_child_locator(tmp_path):
    root = Path(__file__).resolve().parents[1]
    pwsh = shutil.which("pwsh.exe")
    if not pwsh or not (root / ".venv/Scripts/python.exe").is_file():
        pytest.skip("requires project interpreter and reviewed PowerShell 7")
    import hashlib
    paths = ["start_tradingagents.ps1", "scripts/Start-MainlandAnalysis.ps1",
             "scripts/Get-PublicFundflow.ps1", "config/ordinary-full-pair-policy.json",
             ".venv/pyvenv.cfg", ".venv/Scripts/python.exe"]
    for name in paths:
        dest = tmp_path / name
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(root / name, dest)
    policy = json.loads((tmp_path / paths[3]).read_bytes())
    if hashlib.sha256(Path(pwsh).read_bytes()).hexdigest() != policy["executable_sha256"]:
        pytest.skip("host differs from reviewed binary")
    rows = [{"root": "project", "relative": n, "sha256": hashlib.sha256((tmp_path / n).read_bytes()).hexdigest()} for n in paths[2:4]]
    (tmp_path / "config/bounded_mainland_release.json").write_text(json.dumps({"files": rows}))
    probe = PROBE.replace('sys.path.insert(0, str(root))', 'sys.path.insert(0, ' + repr(str(root)) + ')')
    (tmp_path / "scripts/analyze_mainland_bounded.py").write_text(probe)
    key = "TRADINGAGENTS_ORDINARY_FULL_PWSH"
    command = [pwsh, "-NoProfile", "-NonInteractive", "-File", str(tmp_path / paths[0]),
               "-Full", "-Ticker", "601328.SH", "-AnalysisDate", "2026-11-20", "-CheckOnly"]
    env = {k: v for k, v in os.environ.items() if k.lower() in ("systemroot", "windir")}
    # A locator may point to another ordinary file with the reviewed digest.
    # Only the already-running host is invoked. The alternate file is inspected.
    alternate = tmp_path / "alternate" / "pwsh.exe"
    alternate.parent.mkdir()
    shutil.copyfile(pwsh, alternate)
    for explicit in (None, pwsh, str(alternate), "invalid.exe"):
        child = env.copy()
        if explicit is not None:
            child[key] = explicit
        result = subprocess.run(command, env=child, cwd=tmp_path, capture_output=True, timeout=30)
        if explicit == "invalid.exe":
            assert result.returncode and b"full_pair_runtime_unverified" in result.stderr
        else:
            assert result.returncode == 0, result.stderr
            result_data = json.loads(result.stdout)
            assert result_data["registered"] == 2
            assert Path(result_data["locator"]) == Path(explicit or pwsh)
    alternate.write_bytes(b"not the reviewed executable")
    result = subprocess.run(command, env={**env, key: str(alternate)}, cwd=tmp_path,
                            capture_output=True, timeout=30)
    assert result.returncode and b"full_pair_runtime_unverified" in result.stderr
    # Call the public entry in the same interactive host and inspect its environment
    # after the entry script returns.
    same_host = """& ./start_tradingagents.ps1 -Full -Ticker 601328.SH -AnalysisDate 2026-11-20 -CheckOnly
if ($LASTEXITCODE -ne 0 -or (Test-Path Env:TRADINGAGENTS_ORDINARY_FULL_PWSH)) { exit 9 }
"""
    result = subprocess.run([pwsh, "-NoProfile", "-NonInteractive", "-Command", same_host],
                            env=env, cwd=tmp_path, capture_output=True, timeout=30)
    assert result.returncode == 0 and json.loads(result.stdout)["registered"] == 2
    helper = tmp_path / paths[2]
    helper.write_bytes(helper.read_bytes() + b"\n")
    result = subprocess.run(command, env=env, cwd=tmp_path, capture_output=True, timeout=30)
    assert result.returncode and b"full_pair_runtime_unverified" in result.stderr


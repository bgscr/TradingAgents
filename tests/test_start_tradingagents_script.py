import re
import shutil
import subprocess
from pathlib import Path

import pytest


@pytest.mark.unit
def test_start_script_uses_script_relative_project_dir():
    repo_root = Path(__file__).resolve().parents[1]
    script_path = repo_root / "start_tradingagents.ps1"
    powershell = shutil.which("pwsh") or shutil.which("powershell")
    if powershell is None:
        pytest.skip("PowerShell is not available")

    script_text = script_path.read_text(encoding="utf-8")
    assert re.search(r"(?i)\b[a-z]:[\\/]", script_text) is None

    completed = subprocess.run(
        [
            powershell,
            "-NoProfile",
            "-NonInteractive",
            "-File",
            str(script_path),
            "-DryRun",
        ],
        cwd=repo_root.parent,
        text=True,
        capture_output=True,
        timeout=30,
        check=False,
    )

    assert completed.returncode == 0, completed.stderr
    assert f"ProjectDir={repo_root}" in completed.stdout


@pytest.mark.parametrize("folder", ["portable", "portable with spaces"])
def test_portable_dry_run_follows_relocated_launcher(tmp_path, monkeypatch, folder):
    host = shutil.which("pwsh")
    if host is None:
        pytest.skip("PowerShell 7 is required")
    root = tmp_path / folder
    root.mkdir()
    launcher = root / "start_tradingagents.ps1"
    shutil.copyfile(Path(__file__).resolve().parents[1] / launcher.name, launcher)
    (root / "tradingagents.exe").touch()
    overrides = ("TRADINGAGENTS_RESULTS_DIR", "TRADINGAGENTS_CACHE_DIR", "TRADINGAGENTS_MEMORY_LOG_PATH")
    for name in overrides:
        monkeypatch.delenv(name, raising=False)
    completed = subprocess.run(
        [host, "-NoProfile", "-NonInteractive", "-File", str(launcher), "-DryRun"],
        cwd=tmp_path, text=True, capture_output=True, timeout=30, check=True,
    )
    assert "Mode=portable" in completed.stdout
    assert f"ProjectDir={root}" in completed.stdout
    assert f"Launcher={root / 'tradingagents.exe'}" in completed.stdout
    assert not (root / "reports").exists()

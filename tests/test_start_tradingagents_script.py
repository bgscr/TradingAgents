import shutil
import subprocess
from pathlib import Path

import pytest


@pytest.mark.unit
def test_start_script_uses_script_relative_project_dir():
    repo_root = Path(__file__).resolve().parents[1]
    script_path = repo_root / "start_tradingagents.ps1"
    powershell = shutil.which("powershell") or shutil.which("pwsh")
    if powershell is None:
        pytest.skip("PowerShell is not available")

    script_text = script_path.read_text(encoding="utf-8")
    assert "D:\\prj\\TradingAgents\\TradingAgents" not in script_text

    completed = subprocess.run(
        [
            powershell,
            "-NoProfile",
            "-ExecutionPolicy",
            "Bypass",
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

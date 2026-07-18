from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
POWERSHELL = shutil.which("powershell") or shutil.which("pwsh")

pytestmark = pytest.mark.skipif(POWERSHELL is None, reason="PowerShell is required")


def _powershell_base() -> list[str]:
    assert POWERSHELL is not None
    command = [POWERSHELL, "-NoProfile"]
    if os.name == "nt":
        command.extend(["-ExecutionPolicy", "Bypass"])
    return command


def _run_script(script: Path, *args: str, cwd: Path = REPO_ROOT) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [*_powershell_base(), "-File", str(script), *args],
        cwd=cwd,
        check=False,
        text=True,
        capture_output=True,
    )


def _assert_success(result: subprocess.CompletedProcess[str]) -> None:
    assert result.returncode == 0, (
        f"stdout:\n{result.stdout}\n\nstderr:\n{result.stderr}"
    )


@pytest.mark.parametrize(
    "script_name",
    [
        "ak_pick_a_stock.ps1",
        "start_tradingagents.ps1",
        "upgrade_tradingagents.ps1",
        "scripts/build_windows_release.ps1",
    ],
)
def test_powershell_launchers_parse(script_name: str) -> None:
    script = REPO_ROOT / script_name
    escaped_script = str(script).replace("'", "''")
    result = subprocess.run(
        [
            *_powershell_base(),
            "-Command",
            f"[scriptblock]::Create((Get-Content -LiteralPath '{escaped_script}' -Raw)) | Out-Null",
        ],
        check=False,
        text=True,
        capture_output=True,
    )

    _assert_success(result)


def test_ak_picker_launcher_uses_development_mode_from_source_checkout() -> None:
    if (REPO_ROOT / "ak_pick_a_stock.exe").exists():
        pytest.skip("source checkout contains a portable executable")

    result = _run_script(REPO_ROOT / "ak_pick_a_stock.ps1", "-DryRun")

    _assert_success(result)
    assert "Mode=development" in result.stdout
    assert f"ProjectDir={REPO_ROOT}" in result.stdout
    assert f"PythonScript={REPO_ROOT / 'ak_pick_a_stock.py'}" in result.stdout
    venv_python = REPO_ROOT / ".venv" / "Scripts" / "python.exe"
    expected_launcher = str(venv_python) if venv_python.exists() else "python"
    assert f"Launcher={expected_launcher}" in result.stdout


def test_ak_picker_launcher_keeps_portable_mode_when_executable_is_present(
    tmp_path: Path,
) -> None:
    shutil.copy2(REPO_ROOT / "ak_pick_a_stock.ps1", tmp_path / "ak_pick_a_stock.ps1")
    portable_exe = tmp_path / "ak_pick_a_stock.exe"
    portable_exe.write_bytes(b"")

    result = _run_script(tmp_path / "ak_pick_a_stock.ps1", "-DryRun", cwd=tmp_path)

    _assert_success(result)
    assert "Mode=portable" in result.stdout
    assert f"AppDir={tmp_path}" in result.stdout
    assert f"Launcher={portable_exe}" in result.stdout
    assert f"AK_PICK_OUTPUT_PATH={tmp_path / 'reports' / 'ak_candidates.csv'}" in result.stdout


def test_start_launcher_uses_development_mode_from_source_checkout() -> None:
    if (REPO_ROOT / "tradingagents.exe").exists():
        pytest.skip("source checkout contains a portable executable")

    result = _run_script(REPO_ROOT / "start_tradingagents.ps1", "-DryRun")

    _assert_success(result)
    assert "Mode=development" in result.stdout
    assert f"ProjectDir={REPO_ROOT}" in result.stdout
    assert "Launcher=tradingagents" in result.stdout


def test_upgrade_launcher_dry_run_uses_script_directory() -> None:
    result = _run_script(REPO_ROOT / "upgrade_tradingagents.ps1", "-DryRun")

    _assert_success(result)
    assert "Mode=development" in result.stdout
    assert f"ProjectDir={REPO_ROOT}" in result.stdout
    assert f"VenvActivate={REPO_ROOT / '.venv' / 'Scripts' / 'Activate.ps1'}" in result.stdout

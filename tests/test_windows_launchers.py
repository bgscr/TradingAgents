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


def _run_script(
    script: Path,
    *args: str,
    cwd: Path = REPO_ROOT,
    env: dict[str, str] | None = None,
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [*_powershell_base(), "-File", str(script), *args],
        cwd=cwd,
        env=env,
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


def test_start_launcher_uses_project_local_development_roots_from_outside_repo(
    tmp_path: Path,
) -> None:
    if (REPO_ROOT / "tradingagents.exe").exists():
        pytest.skip("source checkout contains a portable executable")

    outside_dir = tmp_path / "outside"
    outside_dir.mkdir()

    result = _run_script(
        REPO_ROOT / "start_tradingagents.ps1",
        "-DryRun",
        cwd=outside_dir,
    )

    _assert_success(result)
    assert f"TRADINGAGENTS_RESULTS_DIR={REPO_ROOT / 'logs'}" in result.stdout
    assert f"TRADINGAGENTS_CACHE_DIR={REPO_ROOT / 'data' / 'cache'}" in result.stdout
    assert (
        "TRADINGAGENTS_MEMORY_LOG_PATH="
        f"{REPO_ROOT / 'data' / 'memory' / 'trading_memory.md'}"
    ) in result.stdout


def test_start_launcher_project_dotenv_overrides_development_roots(
    tmp_path: Path,
) -> None:
    project_dir = tmp_path / "project"
    outside_dir = tmp_path / "outside"
    project_dir.mkdir()
    outside_dir.mkdir()
    launcher = project_dir / "start_tradingagents.ps1"
    shutil.copy2(REPO_ROOT / "start_tradingagents.ps1", launcher)

    configured_results = (tmp_path / "dotenv-results").as_posix()
    configured_cache = (tmp_path / "dotenv-cache").as_posix()
    configured_memory = (tmp_path / "dotenv-memory.md").as_posix()
    (project_dir / ".env").write_text(
        "\n".join(
            (
                f"TRADINGAGENTS_RESULTS_DIR={configured_results}",
                f"TRADINGAGENTS_CACHE_DIR={configured_cache}",
                f"TRADINGAGENTS_MEMORY_LOG_PATH={configured_memory}",
            )
        ),
        encoding="utf-8",
    )
    env = os.environ.copy()
    for name in (
        "TRADINGAGENTS_RESULTS_DIR",
        "TRADINGAGENTS_CACHE_DIR",
        "TRADINGAGENTS_MEMORY_LOG_PATH",
        "TRADINGAGENTS_PROJECT_ROOT",
    ):
        env.pop(name, None)
    env["PYTHONPATH"] = os.pathsep.join(
        filter(None, (str(REPO_ROOT), env.get("PYTHONPATH")))
    )

    result = _run_script(launcher, "-DryRun", cwd=outside_dir, env=env)

    _assert_success(result)
    assert f"TRADINGAGENTS_RESULTS_DIR={configured_results}" in result.stdout
    assert f"TRADINGAGENTS_CACHE_DIR={configured_cache}" in result.stdout
    assert f"TRADINGAGENTS_MEMORY_LOG_PATH={configured_memory}" in result.stdout


def test_start_launcher_preserves_development_process_overrides(
    tmp_path: Path,
) -> None:
    if (REPO_ROOT / "tradingagents.exe").exists():
        pytest.skip("source checkout contains a portable executable")

    outside_dir = tmp_path / "outside"
    outside_dir.mkdir()
    env = os.environ.copy()
    env["TRADINGAGENTS_RESULTS_DIR"] = "caller-results"
    env["TRADINGAGENTS_CACHE_DIR"] = "caller-cache"
    env["TRADINGAGENTS_MEMORY_LOG_PATH"] = "caller-memory.md"

    result = _run_script(
        REPO_ROOT / "start_tradingagents.ps1",
        "-DryRun",
        cwd=outside_dir,
        env=env,
    )

    _assert_success(result)
    assert "TRADINGAGENTS_RESULTS_DIR=caller-results" in result.stdout
    assert "TRADINGAGENTS_CACHE_DIR=caller-cache" in result.stdout
    assert "TRADINGAGENTS_MEMORY_LOG_PATH=caller-memory.md" in result.stdout


def test_start_launcher_preserves_portable_process_overrides(
    tmp_path: Path,
) -> None:
    launcher = tmp_path / "start_tradingagents.ps1"
    shutil.copy2(REPO_ROOT / "start_tradingagents.ps1", launcher)
    (tmp_path / "tradingagents.exe").write_bytes(b"")
    env = os.environ.copy()
    env["TRADINGAGENTS_RESULTS_DIR"] = "caller-results"
    env["TRADINGAGENTS_CACHE_DIR"] = "caller-cache"
    env["TRADINGAGENTS_MEMORY_LOG_PATH"] = "caller-memory.md"

    result = _run_script(launcher, "-DryRun", cwd=tmp_path, env=env)

    _assert_success(result)
    assert "Mode=portable" in result.stdout
    assert "TRADINGAGENTS_RESULTS_DIR=caller-results" in result.stdout
    assert "TRADINGAGENTS_CACHE_DIR=caller-cache" in result.stdout
    assert "TRADINGAGENTS_MEMORY_LOG_PATH=caller-memory.md" in result.stdout


def test_project_local_runtime_roots_are_gitignored() -> None:
    ignored = (REPO_ROOT / ".gitignore").read_text(encoding="utf-8").splitlines()

    assert "/logs/" in ignored
    assert "/data/cache/" in ignored
    assert "/data/memory/" in ignored

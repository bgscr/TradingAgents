# Win11 Portable Release Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build a Windows 11 portable TradingAgents release folder that runs `start_tradingagents.ps1` and `ak_pick_a_stock.py` functionality without target-machine Python, pip, virtualenv, or system installation.

**Architecture:** Use PyInstaller `onedir` packaging with two console executables collected into one release directory. Windows launchers resolve their own directory, set app-local output paths, and start the packaged executables without calling system Python. Python source changes stay narrow: only candidate CSV output path control is added so portable launchers can force `reports\ak_candidates.csv`.

**Tech Stack:** Python 3.10+, PyInstaller, PowerShell, Windows batch launchers, pytest, ruff, existing TradingAgents Typer/Rich CLI, existing `ak_pick_a_stock.py` script.

## Global Constraints

- Target machines run Windows 11.
- Target machines may not have Python or pip installed.
- If Python is installed, its version may not satisfy this project.
- Target machines may not allow installing Python, pip packages, or system-level dependencies.
- Network access and editable API-key configuration are allowed.
- All runtime output must stay under the portable release directory, not under `C:\Users\<user>\.tradingagents` or another user-profile location.
- Preserve `start_tradingagents.ps1` for the TradingAgents CLI entry point.
- Preserve `ak_pick_a_stock.py` functionality for the China A-share candidate picker.
- Provide `.cmd` and `.ps1` launchers for both entry points.
- Keep API keys and runtime configuration editable in a plain `.env` file.
- Do not install or modify Python on the target machine.
- Do not write to system `PATH`, the registry, or global site-packages.
- Do not compile API keys into executables.
- Keep the existing Python development workflow working.
- Prefix shell commands with `rtk` when executing them in this repository.

---

## Scope Check

The approved spec covers one subsystem: a Windows portable release package. It includes packaging, launchers, path control, and verification. These pieces must be implemented together because the package is only successful when the executable, launchers, and local-output contract work as one release artifact.

## File Structure

- `ak_pick_a_stock.py`: owns candidate selection and final CSV writing. Add one environment-variable-controlled output-path helper and route the existing write through it.
- `tests/test_ak_pick_a_stock.py`: extend existing candidate-picker tests with output-path behavior.
- `packaging/pyinstaller/tradingagents_entry.py`: minimal PyInstaller entrypoint for the Typer CLI.
- `packaging/pyinstaller/ak_pick_a_stock_entry.py`: minimal PyInstaller entrypoint for candidate picking.
- `packaging/pyinstaller/tradingagents_portable.spec`: PyInstaller multi-executable `onedir` collection.
- `tests/test_pyinstaller_entrypoints.py`: unit tests for the small entrypoint wrappers.
- `start_tradingagents.ps1`: hybrid launcher. In a release folder it runs `tradingagents.exe`; in a source checkout it retains the current `.venv` development fallback.
- `start_tradingagents.cmd`: double-click wrapper for `start_tradingagents.ps1`.
- `ak_pick_a_stock.ps1`: portable candidate-picker launcher.
- `ak_pick_a_stock.cmd`: double-click wrapper for `ak_pick_a_stock.ps1`.
- `scripts/build_windows_release.ps1`: repeatable Windows build and assembly script.
- `pyproject.toml`: add build optional dependency for PyInstaller.
- `docs/windows-portable-release.md`: short operator guide for building and running the release.

---

### Task 1: Candidate Picker Output Path

**Files:**
- Modify: `ak_pick_a_stock.py`
- Test: `tests/test_ak_pick_a_stock.py`

**Interfaces:**
- Consumes: environment variable `AK_PICK_OUTPUT_PATH`
- Produces: `AK_PICK_OUTPUT_PATH_ENV: str`
- Produces: `resolve_candidate_output_path() -> pathlib.Path`
- Produces: `write_candidates_csv(result: pandas.DataFrame) -> pathlib.Path`

- [ ] **Step 1: Write the failing output-path test**

Add this test near the existing `main()` tests in `tests/test_ak_pick_a_stock.py`, after `test_main_falls_back_to_sina_when_eastmoney_spot_fails`:

```python
def test_main_respects_ak_pick_output_path(monkeypatch, tmp_path):
    def fail_eastmoney():
        raise requests.ConnectionError("remote closed")

    def sina_spot():
        return pd.DataFrame(
            [
                {
                    "代码": "sh600519",
                    "名称": "贵州茅台",
                    "最新价": 1700,
                    "涨跌幅": 2.0,
                    "成交额": 1_200_000_000,
                },
            ]
        )

    work_dir = tmp_path / "work"
    work_dir.mkdir()
    output_path = tmp_path / "reports" / "ak_candidates.csv"

    monkeypatch.setattr(picker, "_fetch_eastmoney_spot_direct", fail_eastmoney)
    monkeypatch.setattr(picker.ak, "stock_zh_a_spot_em", fail_eastmoney)
    monkeypatch.setattr(picker.ak, "stock_zh_a_spot", sina_spot)
    monkeypatch.setenv(picker.AK_PICK_OUTPUT_PATH_ENV, str(output_path))
    monkeypatch.chdir(work_dir)

    assert picker.main() == 0

    result = pd.read_csv(output_path)
    assert list(result["tradingagents_ticker"]) == ["600519.SS"]
    assert not (work_dir / "ak_candidates.csv").exists()
```

- [ ] **Step 2: Run the failing test**

Run:

```powershell
rtk pytest tests/test_ak_pick_a_stock.py::test_main_respects_ak_pick_output_path -q
```

Expected: FAIL because `ak_pick_a_stock.py` does not define `AK_PICK_OUTPUT_PATH_ENV`.

- [ ] **Step 3: Add the output-path helper**

Modify the imports at the top of `ak_pick_a_stock.py`:

```python
from pathlib import Path
```

Add this constant near the other environment-variable constants:

```python
AK_PICK_OUTPUT_PATH_ENV = "AK_PICK_OUTPUT_PATH"
```

Add these helpers before `main()`:

```python
def resolve_candidate_output_path() -> Path:
    raw_path = os.environ.get(AK_PICK_OUTPUT_PATH_ENV, "").strip()
    return Path(raw_path) if raw_path else Path("ak_candidates.csv")


def write_candidates_csv(result: pd.DataFrame) -> Path:
    output_path = resolve_candidate_output_path()
    if output_path.parent != Path("."):
        output_path.parent.mkdir(parents=True, exist_ok=True)
    result.to_csv(output_path, index=False, encoding="utf-8-sig")
    return output_path
```

Replace the hard-coded write in `main()`:

```python
    output_path = write_candidates_csv(result)
    print(f"\n已输出: {output_path}")
```

- [ ] **Step 4: Run focused candidate-picker tests**

Run:

```powershell
rtk pytest tests/test_ak_pick_a_stock.py::test_main_respects_ak_pick_output_path tests/test_ak_pick_a_stock.py::test_main_falls_back_to_sina_when_eastmoney_spot_fails tests/test_ak_pick_a_stock.py::test_main_marks_degraded_candidates_when_spot_source_lacks_valuation -q
```

Expected: all selected tests PASS.

- [ ] **Step 5: Commit Task 1**

Run:

```powershell
rtk git add ak_pick_a_stock.py tests/test_ak_pick_a_stock.py
rtk git commit -m "Support configurable candidate CSV output"
```

---

### Task 2: PyInstaller Entrypoints and Spec

**Files:**
- Create: `packaging/pyinstaller/tradingagents_entry.py`
- Create: `packaging/pyinstaller/ak_pick_a_stock_entry.py`
- Create: `packaging/pyinstaller/tradingagents_portable.spec`
- Create: `tests/test_pyinstaller_entrypoints.py`

**Interfaces:**
- Consumes: `cli.main.app`
- Consumes: `ak_pick_a_stock.main() -> int`
- Produces: `packaging.pyinstaller.tradingagents_entry.get_app()`
- Produces: `packaging.pyinstaller.tradingagents_entry.main() -> None`
- Produces: `packaging.pyinstaller.ak_pick_a_stock_entry.main() -> int`
- Produces: PyInstaller collection named `TradingAgents-Win64`

- [ ] **Step 1: Write entrypoint wrapper tests**

Create `tests/test_pyinstaller_entrypoints.py`:

```python
from __future__ import annotations

import importlib.util
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def _load_module(path: Path, module_name: str):
    spec = importlib.util.spec_from_file_location(module_name, path)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_tradingagents_entry_invokes_typer_app(monkeypatch):
    module = _load_module(
        ROOT / "packaging" / "pyinstaller" / "tradingagents_entry.py",
        "tradingagents_entry_for_test",
    )
    calls = []

    def fake_app():
        calls.append("called")

    monkeypatch.setattr(module, "get_app", lambda: fake_app)

    assert module.main() is None
    assert calls == ["called"]


def test_ak_pick_entry_returns_picker_exit_code(monkeypatch):
    module = _load_module(
        ROOT / "packaging" / "pyinstaller" / "ak_pick_a_stock_entry.py",
        "ak_pick_a_stock_entry_for_test",
    )
    monkeypatch.setattr(module, "pick_main", lambda: 7)

    assert module.main() == 7
```

- [ ] **Step 2: Run the failing entrypoint tests**

Run:

```powershell
rtk pytest tests/test_pyinstaller_entrypoints.py -q
```

Expected: FAIL because the entrypoint files do not exist.

- [ ] **Step 3: Create the TradingAgents CLI entrypoint**

Create `packaging/pyinstaller/tradingagents_entry.py`:

```python
from __future__ import annotations


def get_app():
    from cli.main import app

    return app


def main() -> None:
    app = get_app()
    app()


if __name__ == "__main__":
    main()
```

- [ ] **Step 4: Create the candidate-picker entrypoint**

Create `packaging/pyinstaller/ak_pick_a_stock_entry.py`:

```python
from __future__ import annotations

import sys

from ak_pick_a_stock import main as pick_main


def main() -> int:
    return pick_main()


if __name__ == "__main__":
    sys.exit(main())
```

- [ ] **Step 5: Create the PyInstaller multi-executable spec**

Create `packaging/pyinstaller/tradingagents_portable.spec`:

```python
# -*- mode: python ; coding: utf-8 -*-

from PyInstaller.utils.hooks import collect_data_files, collect_submodules


block_cipher = None


def safe_collect_submodules(package_name):
    try:
        return collect_submodules(package_name)
    except Exception:
        return []


def safe_collect_data_files(package_name):
    try:
        return collect_data_files(package_name)
    except Exception:
        return []


HIDDEN_IMPORT_PACKAGES = [
    "akshare",
    "baostock",
    "certifi",
    "cli",
    "dotenv",
    "langchain_anthropic",
    "langchain_core",
    "langchain_experimental",
    "langchain_google_genai",
    "langchain_openai",
    "langgraph",
    "langgraph_checkpoint",
    "numpy",
    "openai",
    "pandas",
    "pydantic",
    "questionary",
    "redis",
    "requests",
    "rich",
    "stockstats",
    "tqdm",
    "tradingagents",
    "typer",
    "yfinance",
]

hiddenimports = []
for package_name in HIDDEN_IMPORT_PACKAGES:
    hiddenimports += safe_collect_submodules(package_name)

datas = []
for package_name in ["certifi", "cli"]:
    datas += safe_collect_data_files(package_name)


tradingagents_analysis = Analysis(
    ["packaging/pyinstaller/tradingagents_entry.py"],
    pathex=[],
    binaries=[],
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[],
    win_no_prefer_redirects=False,
    win_private_assemblies=False,
    cipher=block_cipher,
    noarchive=False,
)
tradingagents_pyz = PYZ(
    tradingagents_analysis.pure,
    tradingagents_analysis.zipped_data,
    cipher=block_cipher,
)
tradingagents_exe = EXE(
    tradingagents_pyz,
    tradingagents_analysis.scripts,
    [],
    exclude_binaries=True,
    name="tradingagents",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    console=True,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
)

candidate_analysis = Analysis(
    ["packaging/pyinstaller/ak_pick_a_stock_entry.py"],
    pathex=[],
    binaries=[],
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[],
    win_no_prefer_redirects=False,
    win_private_assemblies=False,
    cipher=block_cipher,
    noarchive=False,
)
candidate_pyz = PYZ(
    candidate_analysis.pure,
    candidate_analysis.zipped_data,
    cipher=block_cipher,
)
candidate_exe = EXE(
    candidate_pyz,
    candidate_analysis.scripts,
    [],
    exclude_binaries=True,
    name="ak_pick_a_stock",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    console=True,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
)

coll = COLLECT(
    tradingagents_exe,
    candidate_exe,
    tradingagents_analysis.binaries,
    candidate_analysis.binaries,
    tradingagents_analysis.zipfiles,
    candidate_analysis.zipfiles,
    tradingagents_analysis.datas,
    candidate_analysis.datas,
    strip=False,
    upx=True,
    upx_exclude=[],
    name="TradingAgents-Win64",
)
```

- [ ] **Step 6: Run entrypoint tests**

Run:

```powershell
rtk pytest tests/test_pyinstaller_entrypoints.py -q
```

Expected: PASS.

- [ ] **Step 7: Run import smoke commands**

Run:

```powershell
rtk python packaging/pyinstaller/tradingagents_entry.py --help
rtk python packaging/pyinstaller/ak_pick_a_stock_entry.py
```

Expected:

- The first command prints Typer help and exits 0.
- The second command may fetch live market data. If the network source is unavailable, it may exit 1 with a clear market-data error. It must not fail with `ImportError` or `ModuleNotFoundError`.

- [ ] **Step 8: Commit Task 2**

Run:

```powershell
rtk git add packaging/pyinstaller tests/test_pyinstaller_entrypoints.py
rtk git commit -m "Add PyInstaller portable entrypoints"
```

---

### Task 3: Portable Windows Launchers

**Files:**
- Modify: `start_tradingagents.ps1`
- Create: `start_tradingagents.cmd`
- Create: `ak_pick_a_stock.ps1`
- Create: `ak_pick_a_stock.cmd`

**Interfaces:**
- Consumes: `tradingagents.exe` in the launcher directory for portable mode.
- Consumes: `ak_pick_a_stock.exe` in the launcher directory for candidate-picker portable mode.
- Produces: `TRADINGAGENTS_RESULTS_DIR=<AppDir>\reports\runs`
- Produces: `TRADINGAGENTS_CACHE_DIR=<AppDir>\data\cache`
- Produces: `TRADINGAGENTS_MEMORY_LOG_PATH=<AppDir>\data\memory\trading_memory.md`
- Produces: `AK_PICK_OUTPUT_PATH=<AppDir>\reports\ak_candidates.csv`

- [ ] **Step 1: Replace `start_tradingagents.ps1` with a hybrid launcher**

Use this complete content for `start_tradingagents.ps1`:

```powershell
param(
    [switch]$DryRun
)

$ErrorActionPreference = "Stop"

$ScriptDir = if ($PSScriptRoot) {
    $PSScriptRoot
}
else {
    Split-Path -Parent $MyInvocation.MyCommand.Path
}

$AppDir = (Resolve-Path -LiteralPath $ScriptDir).Path
$TradingAgentsExe = Join-Path $AppDir "tradingagents.exe"
$VenvActivate = Join-Path $AppDir ".venv\Scripts\Activate.ps1"

function Ensure-Directory {
    param([Parameter(Mandatory = $true)][string]$Path)
    if (-not (Test-Path -LiteralPath $Path -PathType Container)) {
        New-Item -ItemType Directory -Path $Path -Force | Out-Null
    }
}

function Set-PortableTradingAgentsEnvironment {
    param([Parameter(Mandatory = $true)][string]$Root)

    $ReportsDir = Join-Path $Root "reports\runs"
    $CacheDir = Join-Path $Root "data\cache"
    $MemoryDir = Join-Path $Root "data\memory"
    $LogsDir = Join-Path $Root "logs"

    Ensure-Directory $ReportsDir
    Ensure-Directory $CacheDir
    Ensure-Directory $MemoryDir
    Ensure-Directory $LogsDir

    $env:TRADINGAGENTS_RESULTS_DIR = $ReportsDir
    $env:TRADINGAGENTS_CACHE_DIR = $CacheDir
    $env:TRADINGAGENTS_MEMORY_LOG_PATH = Join-Path $MemoryDir "trading_memory.md"
}

if (Test-Path -LiteralPath $TradingAgentsExe -PathType Leaf) {
    Set-PortableTradingAgentsEnvironment $AppDir
    Set-Location -LiteralPath $AppDir

    if ($DryRun) {
        Write-Host "Mode=portable"
        Write-Host "AppDir=$AppDir"
        Write-Host "Launcher=$TradingAgentsExe"
        Write-Host "TRADINGAGENTS_RESULTS_DIR=$env:TRADINGAGENTS_RESULTS_DIR"
        Write-Host "TRADINGAGENTS_CACHE_DIR=$env:TRADINGAGENTS_CACHE_DIR"
        Write-Host "TRADINGAGENTS_MEMORY_LOG_PATH=$env:TRADINGAGENTS_MEMORY_LOG_PATH"
        exit 0
    }

    Write-Host "Starting TradingAgents portable release..." -ForegroundColor Green
    & $TradingAgentsExe @args
    exit $LASTEXITCODE
}

if ($DryRun) {
    Write-Host "Mode=development"
    Write-Host "AppDir=$AppDir"
    Write-Host "VenvActivate=$VenvActivate"
    Write-Host "Launcher=tradingagents"
    exit 0
}

if (-not (Test-Path -LiteralPath $VenvActivate -PathType Leaf)) {
    Write-Host "Virtual environment does not exist: $VenvActivate" -ForegroundColor Red
    Write-Host "Run first: python -m venv .venv; .\.venv\Scripts\python.exe -m pip install -e ." -ForegroundColor Yellow
    exit 1
}

Set-Location -LiteralPath $AppDir
. $VenvActivate

Write-Host "Starting TradingAgents development environment..." -ForegroundColor Green

try {
    tradingagents @args
}
catch {
    Write-Host "tradingagents command failed; falling back to python -m cli.main..." -ForegroundColor Yellow
    python -m cli.main @args
}
```

- [ ] **Step 2: Add the TradingAgents `.cmd` wrapper**

Create `start_tradingagents.cmd`:

```bat
@echo off
setlocal

powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%~dp0start_tradingagents.ps1" %*
set EXITCODE=%ERRORLEVEL%

if not "%EXITCODE%"=="0" (
  echo.
  echo TradingAgents exited with code %EXITCODE%.
  pause
)

exit /b %EXITCODE%
```

- [ ] **Step 3: Add the candidate-picker PowerShell launcher**

Create `ak_pick_a_stock.ps1`:

```powershell
param(
    [switch]$DryRun
)

$ErrorActionPreference = "Stop"

$ScriptDir = if ($PSScriptRoot) {
    $PSScriptRoot
}
else {
    Split-Path -Parent $MyInvocation.MyCommand.Path
}

$AppDir = (Resolve-Path -LiteralPath $ScriptDir).Path
$CandidateExe = Join-Path $AppDir "ak_pick_a_stock.exe"
$ReportsDir = Join-Path $AppDir "reports"
$LogsDir = Join-Path $AppDir "logs"

function Ensure-Directory {
    param([Parameter(Mandatory = $true)][string]$Path)
    if (-not (Test-Path -LiteralPath $Path -PathType Container)) {
        New-Item -ItemType Directory -Path $Path -Force | Out-Null
    }
}

Ensure-Directory $ReportsDir
Ensure-Directory $LogsDir

$env:AK_PICK_OUTPUT_PATH = Join-Path $ReportsDir "ak_candidates.csv"

if ($DryRun) {
    Write-Host "Mode=portable"
    Write-Host "AppDir=$AppDir"
    Write-Host "Launcher=$CandidateExe"
    Write-Host "AK_PICK_OUTPUT_PATH=$env:AK_PICK_OUTPUT_PATH"
    exit 0
}

if (-not (Test-Path -LiteralPath $CandidateExe -PathType Leaf)) {
    Write-Host "Candidate picker executable does not exist: $CandidateExe" -ForegroundColor Red
    exit 1
}

Set-Location -LiteralPath $AppDir

Write-Host "Starting A-share candidate picker..." -ForegroundColor Green
& $CandidateExe @args
exit $LASTEXITCODE
```

- [ ] **Step 4: Add the candidate-picker `.cmd` wrapper**

Create `ak_pick_a_stock.cmd`:

```bat
@echo off
setlocal

powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%~dp0ak_pick_a_stock.ps1" %*
set EXITCODE=%ERRORLEVEL%

if not "%EXITCODE%"=="0" (
  echo.
  echo A-share candidate picker exited with code %EXITCODE%.
  pause
)

exit /b %EXITCODE%
```

- [ ] **Step 5: Verify launcher dry runs from the source checkout**

Run:

```powershell
rtk powershell -NoProfile -ExecutionPolicy Bypass -File .\start_tradingagents.ps1 -DryRun
rtk powershell -NoProfile -ExecutionPolicy Bypass -File .\ak_pick_a_stock.ps1 -DryRun
```

Expected:

- The first command prints `Mode=development` in the source checkout unless a `tradingagents.exe` exists at the repo root.
- The second command prints `Mode=portable` and `AK_PICK_OUTPUT_PATH=<repo>\reports\ak_candidates.csv`.

- [ ] **Step 6: Commit Task 3**

Run:

```powershell
rtk git add start_tradingagents.ps1 start_tradingagents.cmd ak_pick_a_stock.ps1 ak_pick_a_stock.cmd
rtk git commit -m "Add portable Windows launchers"
```

---

### Task 4: Windows Release Build Script

**Files:**
- Modify: `pyproject.toml`
- Create: `scripts/build_windows_release.ps1`

**Interfaces:**
- Consumes: `packaging/pyinstaller/tradingagents_portable.spec`
- Consumes: root launchers from Task 3
- Produces: `dist\TradingAgents-Win64`
- Produces: `dist\TradingAgents-Win64\.env`
- Produces: `dist\TradingAgents-Win64\data\cache`
- Produces: `dist\TradingAgents-Win64\data\memory`
- Produces: `dist\TradingAgents-Win64\logs`
- Produces: `dist\TradingAgents-Win64\reports\runs`

- [ ] **Step 1: Add the build optional dependency**

Add this optional dependency group in `pyproject.toml` under `[project.optional-dependencies]`:

```toml
build = [
    "pyinstaller>=6.10",
]
```

- [ ] **Step 2: Create the build script**

Create `scripts/build_windows_release.ps1`:

```powershell
param(
    [string]$ReleaseName = "TradingAgents-Win64",
    [string]$DistRoot = "dist",
    [switch]$IncludeLocalEnv,
    [switch]$SkipSmoke
)

$ErrorActionPreference = "Stop"

$ScriptDir = if ($PSScriptRoot) {
    $PSScriptRoot
}
else {
    Split-Path -Parent $MyInvocation.MyCommand.Path
}
$RepoRoot = (Resolve-Path -LiteralPath (Join-Path $ScriptDir "..")).Path
$DistRootPath = Join-Path $RepoRoot $DistRoot
$ReleaseDir = Join-Path $DistRootPath $ReleaseName
$WorkPath = Join-Path $RepoRoot "build\pyinstaller"
$SpecPath = Join-Path $RepoRoot "packaging\pyinstaller\tradingagents_portable.spec"

function Invoke-Checked {
    param(
        [Parameter(Mandatory = $true)][string]$FilePath,
        [Parameter(Mandatory = $true)][string[]]$Arguments
    )

    Write-Host "> $FilePath $($Arguments -join ' ')" -ForegroundColor Cyan
    & $FilePath @Arguments
    if ($LASTEXITCODE -ne 0) {
        throw "Command failed with exit code $LASTEXITCODE`: $FilePath $($Arguments -join ' ')"
    }
}

function Ensure-Directory {
    param([Parameter(Mandatory = $true)][string]$Path)
    if (-not (Test-Path -LiteralPath $Path -PathType Container)) {
        New-Item -ItemType Directory -Path $Path -Force | Out-Null
    }
}

function Copy-RequiredFile {
    param(
        [Parameter(Mandatory = $true)][string]$Source,
        [Parameter(Mandatory = $true)][string]$Destination
    )
    if (-not (Test-Path -LiteralPath $Source -PathType Leaf)) {
        throw "Required file does not exist: $Source"
    }
    Copy-Item -LiteralPath $Source -Destination $Destination -Force
}

$IsWindowsRuntime = [System.Runtime.InteropServices.RuntimeInformation]::IsOSPlatform(
    [System.Runtime.InteropServices.OSPlatform]::Windows
)
if (-not $IsWindowsRuntime) {
    throw "Windows release builds must run on Windows."
}

Set-Location -LiteralPath $RepoRoot

Invoke-Checked "python" @(
    "-c",
    "import sys; raise SystemExit(0 if sys.version_info >= (3, 10) else 1)"
)
Invoke-Checked "python" @("-m", "pip", "install", "-e", ".[build]")

if (Test-Path -LiteralPath $ReleaseDir) {
    Remove-Item -LiteralPath $ReleaseDir -Recurse -Force
}

Invoke-Checked "python" @(
    "-m",
    "PyInstaller",
    "--noconfirm",
    "--clean",
    "--distpath",
    $DistRootPath,
    "--workpath",
    $WorkPath,
    $SpecPath
)

if (-not (Test-Path -LiteralPath $ReleaseDir -PathType Container)) {
    throw "PyInstaller did not create expected release directory: $ReleaseDir"
}

foreach ($fileName in @(
    "start_tradingagents.ps1",
    "start_tradingagents.cmd",
    "ak_pick_a_stock.ps1",
    "ak_pick_a_stock.cmd"
)) {
    Copy-RequiredFile (Join-Path $RepoRoot $fileName) (Join-Path $ReleaseDir $fileName)
}

$EnvSource = Join-Path $RepoRoot ".env.example"
if ($IncludeLocalEnv) {
    $LocalEnv = Join-Path $RepoRoot ".env"
    if (Test-Path -LiteralPath $LocalEnv -PathType Leaf) {
        $EnvSource = $LocalEnv
    }
}
Copy-RequiredFile $EnvSource (Join-Path $ReleaseDir ".env")

foreach ($dir in @(
    "data\cache",
    "data\memory",
    "logs",
    "reports",
    "reports\runs"
)) {
    Ensure-Directory (Join-Path $ReleaseDir $dir)
}

if (-not $SkipSmoke) {
    Invoke-Checked "powershell.exe" @(
        "-NoProfile",
        "-ExecutionPolicy",
        "Bypass",
        "-File",
        (Join-Path $ReleaseDir "start_tradingagents.ps1"),
        "-DryRun"
    )
    Invoke-Checked "powershell.exe" @(
        "-NoProfile",
        "-ExecutionPolicy",
        "Bypass",
        "-File",
        (Join-Path $ReleaseDir "ak_pick_a_stock.ps1"),
        "-DryRun"
    )
}

Write-Host "Release created: $ReleaseDir" -ForegroundColor Green
```

- [ ] **Step 3: Run a syntax-level build-script check**

Run:

```powershell
rtk powershell -NoProfile -Command "$null = [scriptblock]::Create((Get-Content -LiteralPath 'scripts\build_windows_release.ps1' -Raw)); 'syntax ok'"
```

Expected: prints `syntax ok`.

- [ ] **Step 4: Verify PyInstaller is available through the build extra**

Run:

```powershell
rtk python -m pip install -e ".[build]"
rtk python -m PyInstaller --version
```

Expected: both commands exit 0; the second prints a PyInstaller version.

- [ ] **Step 5: Commit Task 4**

Run:

```powershell
rtk git add pyproject.toml scripts/build_windows_release.ps1
rtk git commit -m "Add Windows portable release build script"
```

---

### Task 5: Full Verification and Operator Documentation

**Files:**
- Create: `docs/windows-portable-release.md`

**Interfaces:**
- Consumes: all outputs from Tasks 1-4
- Produces: operator instructions for building, copying, configuring, and running the portable release

- [ ] **Step 1: Create the operator guide**

Create `docs/windows-portable-release.md`:

````markdown
# Windows Portable Release

This guide builds a Windows 11 portable TradingAgents folder that does not
require Python, pip, virtualenv, or package installation on the target machine.

## Build Machine Requirements

- Windows 11
- Python 3.10 or newer available on the build machine
- Network access for Python package installation

## Build

From the repository root:

```powershell
.\scripts\build_windows_release.ps1
```

The output folder is:

```text
dist\TradingAgents-Win64
```

For a private internal package that copies the local `.env` into the release:

```powershell
.\scripts\build_windows_release.ps1 -IncludeLocalEnv
```

## Target Machine Requirements

- Windows 11
- Network access to the selected LLM provider and market data sources
- Editable `.env` file in the release folder

The target machine does not need Python, pip, or virtualenv.

## Run TradingAgents

Double-click:

```text
start_tradingagents.cmd
```

Advanced users may run:

```powershell
.\start_tradingagents.ps1
```

## Run Candidate Picker

Double-click:

```text
ak_pick_a_stock.cmd
```

Advanced users may run:

```powershell
.\ak_pick_a_stock.ps1
```

The candidate CSV is written to:

```text
reports\ak_candidates.csv
```

## Runtime Output Locations

All runtime output is written under the release folder:

```text
data\cache
data\memory\trading_memory.md
logs
reports
reports\runs
```

The portable launchers set these environment variables before starting the app:

```text
TRADINGAGENTS_RESULTS_DIR
TRADINGAGENTS_CACHE_DIR
TRADINGAGENTS_MEMORY_LOG_PATH
AK_PICK_OUTPUT_PATH
```

## API Keys

Edit `.env` in the release folder with Notepad or another text editor. API keys
are not compiled into the executables.

## Smoke Check

From the release folder:

```powershell
.\start_tradingagents.ps1 -DryRun
.\ak_pick_a_stock.ps1 -DryRun
```

Both commands should print paths under the release folder.
````

- [ ] **Step 2: Run focused unit tests**

Run:

```powershell
rtk pytest tests/test_ak_pick_a_stock.py::test_main_respects_ak_pick_output_path tests/test_pyinstaller_entrypoints.py -q
```

Expected: PASS.

- [ ] **Step 3: Run lint on changed Python files**

Run:

```powershell
rtk ruff check ak_pick_a_stock.py tests/test_ak_pick_a_stock.py tests/test_pyinstaller_entrypoints.py packaging/pyinstaller/tradingagents_entry.py packaging/pyinstaller/ak_pick_a_stock_entry.py
```

Expected: PASS.

- [ ] **Step 4: Run launcher dry runs**

Run:

```powershell
rtk powershell -NoProfile -ExecutionPolicy Bypass -File .\start_tradingagents.ps1 -DryRun
rtk powershell -NoProfile -ExecutionPolicy Bypass -File .\ak_pick_a_stock.ps1 -DryRun
```

Expected:

- `start_tradingagents.ps1 -DryRun` prints app-local paths in portable mode when run from a built release folder, and development mode when run from the source checkout.
- `ak_pick_a_stock.ps1 -DryRun` prints `AK_PICK_OUTPUT_PATH` under the current folder's `reports` directory.

- [ ] **Step 5: Build the release**

Run:

```powershell
rtk powershell -NoProfile -ExecutionPolicy Bypass -File .\scripts\build_windows_release.ps1
```

Expected:

- `dist\TradingAgents-Win64\tradingagents.exe` exists.
- `dist\TradingAgents-Win64\ak_pick_a_stock.exe` exists.
- `dist\TradingAgents-Win64\_internal` exists.
- `dist\TradingAgents-Win64\.env` exists.
- `dist\TradingAgents-Win64\data\cache` exists.
- `dist\TradingAgents-Win64\data\memory` exists.
- `dist\TradingAgents-Win64\logs` exists.
- `dist\TradingAgents-Win64\reports\runs` exists.

- [ ] **Step 6: Run release-folder dry runs**

Run:

```powershell
rtk powershell -NoProfile -ExecutionPolicy Bypass -File .\dist\TradingAgents-Win64\start_tradingagents.ps1 -DryRun
rtk powershell -NoProfile -ExecutionPolicy Bypass -File .\dist\TradingAgents-Win64\ak_pick_a_stock.ps1 -DryRun
```

Expected:

- The first command prints `Mode=portable`.
- The first command prints `TRADINGAGENTS_RESULTS_DIR=<repo>\dist\TradingAgents-Win64\reports\runs`.
- The first command prints `TRADINGAGENTS_CACHE_DIR=<repo>\dist\TradingAgents-Win64\data\cache`.
- The first command prints `TRADINGAGENTS_MEMORY_LOG_PATH=<repo>\dist\TradingAgents-Win64\data\memory\trading_memory.md`.
- The second command prints `AK_PICK_OUTPUT_PATH=<repo>\dist\TradingAgents-Win64\reports\ak_candidates.csv`.

- [ ] **Step 7: Check that release dry runs do not create user-profile output**

Run:

```powershell
rtk powershell -NoProfile -Command "$before = Test-Path -LiteralPath \"$HOME\.tradingagents\"; .\dist\TradingAgents-Win64\start_tradingagents.ps1 -DryRun | Out-Null; .\dist\TradingAgents-Win64\ak_pick_a_stock.ps1 -DryRun | Out-Null; $after = Test-Path -LiteralPath \"$HOME\.tradingagents\"; if ($before -ne $after) { throw 'Dry runs changed user-profile TradingAgents state' }; 'user-profile state unchanged'"
```

Expected: prints `user-profile state unchanged`.

- [ ] **Step 8: Commit Task 5**

Run:

```powershell
rtk git add docs/windows-portable-release.md
rtk git commit -m "Document Windows portable release"
```

---

## Final Review Gate

After all tasks are complete, run:

```powershell
rtk git status --short
rtk pytest tests/test_ak_pick_a_stock.py::test_main_respects_ak_pick_output_path tests/test_pyinstaller_entrypoints.py -q
rtk ruff check ak_pick_a_stock.py tests/test_ak_pick_a_stock.py tests/test_pyinstaller_entrypoints.py packaging/pyinstaller/tradingagents_entry.py packaging/pyinstaller/ak_pick_a_stock_entry.py
rtk powershell -NoProfile -ExecutionPolicy Bypass -File .\scripts\build_windows_release.ps1
rtk powershell -NoProfile -ExecutionPolicy Bypass -File .\dist\TradingAgents-Win64\start_tradingagents.ps1 -DryRun
rtk powershell -NoProfile -ExecutionPolicy Bypass -File .\dist\TradingAgents-Win64\ak_pick_a_stock.ps1 -DryRun
```

Expected:

- Git status is clean after the final task commit.
- Focused pytest tests pass.
- Ruff passes.
- Build completes and creates `dist\TradingAgents-Win64`.
- Release dry runs print only paths under `dist\TradingAgents-Win64`.

## Spec Coverage Self-Review

- No target Python/pip dependency: Tasks 2 and 4 package executables with PyInstaller.
- No system installation: Tasks 3 and 4 use local launchers and release-folder assembly only.
- Both required entry points: Tasks 2 and 3 create `tradingagents.exe`, `ak_pick_a_stock.exe`, `.ps1`, and `.cmd` launchers.
- Editable API keys: Task 4 copies `.env.example` or explicitly requested local `.env`; Task 5 documents editing `.env`.
- Local output only: Tasks 1 and 3 set candidate, report, cache, memory, and log paths under the release folder.
- Repeatable build: Task 4 creates `scripts/build_windows_release.ps1`.
- Verification: Task 5 builds the release and checks launcher dry-run paths.

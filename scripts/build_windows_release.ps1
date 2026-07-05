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
    $ResolvedDistRoot = (Resolve-Path -LiteralPath $DistRootPath).Path
    $ResolvedReleaseDir = (Resolve-Path -LiteralPath $ReleaseDir).Path
    $ResolvedDistRootPrefix = $ResolvedDistRoot.TrimEnd('\') + '\'
    if (-not $ResolvedReleaseDir.StartsWith($ResolvedDistRootPrefix, [System.StringComparison]::OrdinalIgnoreCase)) {
        throw "Refusing to remove release directory outside dist root: $ResolvedReleaseDir"
    }
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

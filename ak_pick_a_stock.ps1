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
$CandidateScript = Join-Path $AppDir "ak_pick_a_stock.py"
$VenvPython = Join-Path $AppDir ".venv\Scripts\python.exe"
$ReportsDir = Join-Path $AppDir "reports"
$LogsDir = Join-Path $AppDir "logs"

function Ensure-Directory {
    param([Parameter(Mandatory = $true)][string]$Path)
    if (-not (Test-Path -LiteralPath $Path -PathType Container)) {
        New-Item -ItemType Directory -Path $Path -Force | Out-Null
    }
}

$env:AK_PICK_OUTPUT_PATH = Join-Path $ReportsDir "ak_candidates.csv"

if (Test-Path -LiteralPath $CandidateExe -PathType Leaf) {
    if ($DryRun) {
        Write-Host "Mode=portable"
        Write-Host "AppDir=$AppDir"
        Write-Host "Launcher=$CandidateExe"
        Write-Host "AK_PICK_OUTPUT_PATH=$env:AK_PICK_OUTPUT_PATH"
        exit 0
    }

    Ensure-Directory $ReportsDir
    Ensure-Directory $LogsDir
    Set-Location -LiteralPath $AppDir

    Write-Host "Starting A-share candidate picker portable release..." -ForegroundColor Green
    & $CandidateExe @args
    exit $LASTEXITCODE
}

if (-not (Test-Path -LiteralPath $CandidateScript -PathType Leaf)) {
    Write-Host "Neither candidate picker executable nor source script exists in: $AppDir" -ForegroundColor Red
    Write-Host "Expected one of: $CandidateExe or $CandidateScript" -ForegroundColor Yellow
    exit 1
}

$PythonLauncher = if (Test-Path -LiteralPath $VenvPython -PathType Leaf) {
    $VenvPython
}
else {
    "python"
}

if ($DryRun) {
    Write-Host "Mode=development"
    Write-Host "ProjectDir=$AppDir"
    Write-Host "AppDir=$AppDir"
    Write-Host "PythonScript=$CandidateScript"
    Write-Host "Launcher=$PythonLauncher"
    Write-Host "AK_PICK_OUTPUT_PATH=$env:AK_PICK_OUTPUT_PATH"
    exit 0
}

Ensure-Directory $ReportsDir
Ensure-Directory $LogsDir

Set-Location -LiteralPath $AppDir

Write-Host "Starting A-share candidate picker development environment..." -ForegroundColor Green
& $PythonLauncher $CandidateScript @args
exit $LASTEXITCODE

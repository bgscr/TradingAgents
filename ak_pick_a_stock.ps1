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

$env:AK_PICK_OUTPUT_PATH = Join-Path $ReportsDir "ak_candidates.csv"

if ($DryRun) {
    Write-Host "Mode=portable"
    Write-Host "AppDir=$AppDir"
    Write-Host "Launcher=$CandidateExe"
    Write-Host "AK_PICK_OUTPUT_PATH=$env:AK_PICK_OUTPUT_PATH"
    exit 0
}

Ensure-Directory $ReportsDir
Ensure-Directory $LogsDir

if (-not (Test-Path -LiteralPath $CandidateExe -PathType Leaf)) {
    Write-Host "Candidate picker executable does not exist: $CandidateExe" -ForegroundColor Red
    exit 1
}

Set-Location -LiteralPath $AppDir

Write-Host "Starting A-share candidate picker..." -ForegroundColor Green
& $CandidateExe @args
exit $LASTEXITCODE

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

$ProjectDir = (Resolve-Path -LiteralPath $ScriptDir).Path
$VenvActivate = Join-Path $ProjectDir ".venv\Scripts\Activate.ps1"

if (-not (Test-Path -LiteralPath $ProjectDir -PathType Container)) {
    Write-Host "Project directory does not exist: $ProjectDir" -ForegroundColor Red
    exit 1
}

if ($DryRun) {
    Write-Host "ProjectDir=$ProjectDir"
    Write-Host "VenvActivate=$VenvActivate"
    Write-Host "Launcher=tradingagents"
    exit 0
}

if (-not (Test-Path -LiteralPath $VenvActivate -PathType Leaf)) {
    Write-Host "Virtual environment does not exist: $VenvActivate" -ForegroundColor Red
    Write-Host "Run first: python -m venv .venv; .\.venv\Scripts\python.exe -m pip install -e ." -ForegroundColor Yellow
    exit 1
}

Set-Location -LiteralPath $ProjectDir
. $VenvActivate

Write-Host "Starting TradingAgents..." -ForegroundColor Green

try {
    tradingagents
}
catch {
    Write-Host "tradingagents command failed; falling back to python -m cli.main..." -ForegroundColor Yellow
    python -m cli.main
}

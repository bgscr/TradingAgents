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
    param(
        [Parameter(Mandatory = $true)][string]$Root,
        [switch]$CreateDirectories
    )

    $ReportsDir = Join-Path $Root "reports\runs"
    $CacheDir = Join-Path $Root "data\cache"
    $MemoryDir = Join-Path $Root "data\memory"
    $LogsDir = Join-Path $Root "logs"

    if ($CreateDirectories) {
        Ensure-Directory $ReportsDir
        Ensure-Directory $CacheDir
        Ensure-Directory $MemoryDir
        Ensure-Directory $LogsDir
    }

    $env:TRADINGAGENTS_RESULTS_DIR = $ReportsDir
    $env:TRADINGAGENTS_CACHE_DIR = $CacheDir
    $env:TRADINGAGENTS_MEMORY_LOG_PATH = Join-Path $MemoryDir "trading_memory.md"
}

if (Test-Path -LiteralPath $TradingAgentsExe -PathType Leaf) {
    Set-PortableTradingAgentsEnvironment -Root $AppDir

    if ($DryRun) {
        Write-Host "Mode=portable"
        Write-Host "ProjectDir=$AppDir"
        Write-Host "AppDir=$AppDir"
        Write-Host "Launcher=$TradingAgentsExe"
        Write-Host "TRADINGAGENTS_RESULTS_DIR=$env:TRADINGAGENTS_RESULTS_DIR"
        Write-Host "TRADINGAGENTS_CACHE_DIR=$env:TRADINGAGENTS_CACHE_DIR"
        Write-Host "TRADINGAGENTS_MEMORY_LOG_PATH=$env:TRADINGAGENTS_MEMORY_LOG_PATH"
        exit 0
    }

    Set-PortableTradingAgentsEnvironment -Root $AppDir -CreateDirectories
    Set-Location -LiteralPath $AppDir

    Write-Host "Starting TradingAgents portable release..." -ForegroundColor Green
    & $TradingAgentsExe @args
    exit $LASTEXITCODE
}

if ($DryRun) {
    Write-Host "Mode=development"
    Write-Host "ProjectDir=$AppDir"
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

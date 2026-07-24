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
$VenvPython = Join-Path $AppDir ".venv\Scripts\python.exe"

function Ensure-Directory {
    param([Parameter(Mandatory = $true)][string]$Path)
    if (-not (Test-Path -LiteralPath $Path -PathType Container)) {
        New-Item -ItemType Directory -Path $Path -Force | Out-Null
    }
}

function Get-ConfiguredRuntimePath {
    param(
        [Parameter(Mandatory = $true)][string]$EnvironmentName,
        [Parameter(Mandatory = $true)][string]$DefaultPath
    )

    $ConfiguredPath = [Environment]::GetEnvironmentVariable(
        $EnvironmentName,
        [EnvironmentVariableTarget]::Process
    )
    if ([string]::IsNullOrWhiteSpace($ConfiguredPath)) {
        return $DefaultPath
    }
    return $ConfiguredPath
}

function Set-PortableTradingAgentsEnvironment {
    param(
        [Parameter(Mandatory = $true)][string]$Root,
        [switch]$CreateDirectories
    )

    $ReportsDir = Get-ConfiguredRuntimePath `
        -EnvironmentName "TRADINGAGENTS_RESULTS_DIR" `
        -DefaultPath (Join-Path $Root "reports\runs")
    $CacheDir = Get-ConfiguredRuntimePath `
        -EnvironmentName "TRADINGAGENTS_CACHE_DIR" `
        -DefaultPath (Join-Path $Root "data\cache")
    $MemoryLogPath = Get-ConfiguredRuntimePath `
        -EnvironmentName "TRADINGAGENTS_MEMORY_LOG_PATH" `
        -DefaultPath (Join-Path $Root "data\memory\trading_memory.md")
    $MemoryDir = Split-Path -Parent $MemoryLogPath
    $LogsDir = Join-Path $Root "logs"

    if ($CreateDirectories) {
        Ensure-Directory $ReportsDir
        Ensure-Directory $CacheDir
        if (-not [string]::IsNullOrWhiteSpace($MemoryDir)) {
            Ensure-Directory $MemoryDir
        }
        Ensure-Directory $LogsDir
    }

    $env:TRADINGAGENTS_RESULTS_DIR = $ReportsDir
    $env:TRADINGAGENTS_CACHE_DIR = $CacheDir
    $env:TRADINGAGENTS_MEMORY_LOG_PATH = $MemoryLogPath
}

function Get-DevelopmentRuntimeConfiguration {
    param(
        [Parameter(Mandatory = $true)][string]$Root
    )

    $PythonLauncher = if (Test-Path -LiteralPath $VenvPython -PathType Leaf) {
        $VenvPython
    }
    else {
        "python"
    }
    $ConfigCode = @'
from tradingagents.default_config import DEFAULT_CONFIG
print(DEFAULT_CONFIG["results_dir"])
print(DEFAULT_CONFIG["data_cache_dir"])
print(DEFAULT_CONFIG["memory_log_path"])
'@
    $EncodedConfigCode = [Convert]::ToBase64String(
        [Text.Encoding]::UTF8.GetBytes($ConfigCode)
    )
    $BootstrapCode = "import sys,base64;exec(base64.b64decode(sys.argv[1]))"

    Push-Location -LiteralPath $Root
    try {
        $Configuration = @(
            & $PythonLauncher -c $BootstrapCode $EncodedConfigCode
        )
        if ($LASTEXITCODE -ne 0) {
            throw "Unable to resolve development runtime configuration."
        }
        if ($Configuration.Count -ne 3) {
            throw "Development runtime configuration returned an unexpected result."
        }
        return @(
            "TRADINGAGENTS_RESULTS_DIR=$($Configuration[0])",
            "TRADINGAGENTS_CACHE_DIR=$($Configuration[1])",
            "TRADINGAGENTS_MEMORY_LOG_PATH=$($Configuration[2])"
        )
    }
    finally {
        Pop-Location
    }
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

$env:TRADINGAGENTS_PROJECT_ROOT = $AppDir

if ($DryRun) {
    Write-Host "Mode=development"
    Write-Host "ProjectDir=$AppDir"
    Write-Host "AppDir=$AppDir"
    Write-Host "VenvActivate=$VenvActivate"
    Write-Host "Launcher=tradingagents"
    Get-DevelopmentRuntimeConfiguration -Root $AppDir |
        ForEach-Object { Write-Host $_ }
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

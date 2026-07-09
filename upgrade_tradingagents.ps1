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

if (-not (Test-Path -LiteralPath $ProjectDir -PathType Container)) {
    Write-Host "Project directory does not exist: $ProjectDir" -ForegroundColor Red
    exit 1
}

if (-not (Test-Path -LiteralPath (Join-Path $ProjectDir ".git"))) {
    Write-Host "Project directory is not a Git repository: $ProjectDir" -ForegroundColor Red
    exit 1
}

if ($DryRun) {
    Write-Host "Mode=development"
    Write-Host "ProjectDir=$ProjectDir"
    Write-Host "VenvActivate=$VenvActivate"
    Write-Host "GitDir=$(Join-Path $ProjectDir ".git")"
    exit 0
}

if (-not (Test-Path -LiteralPath $VenvActivate -PathType Leaf)) {
    Write-Host "Virtual environment does not exist: $VenvActivate" -ForegroundColor Red
    Write-Host "Run first: python -m venv .venv" -ForegroundColor Yellow
    exit 1
}

Set-Location -LiteralPath $ProjectDir

Write-Host "Pulling latest code..." -ForegroundColor Green
Invoke-Checked "git" @("pull")

Write-Host "Activating virtual environment..." -ForegroundColor Green
. $VenvActivate

Write-Host "Reinstalling local package..." -ForegroundColor Green
Invoke-Checked "python" @("-m", "pip", "install", "-e", ".")

Write-Host "Update completed." -ForegroundColor Green

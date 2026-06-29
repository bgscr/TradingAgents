$ErrorActionPreference = "Stop"

$ProjectDir = "D:\prj\TradingAgents\TradingAgents"
$VenvActivate = Join-Path $ProjectDir ".venv\Scripts\Activate.ps1"

if (-not (Test-Path $ProjectDir)) {
    Write-Host "项目目录不存在: $ProjectDir" -ForegroundColor Red
    exit 1
}

if (-not (Test-Path $VenvActivate)) {
    Write-Host "虚拟环境不存在: $VenvActivate" -ForegroundColor Red
    Write-Host "请先执行: python -m venv .venv && pip install -e ." -ForegroundColor Yellow
    exit 1
}

Set-Location $ProjectDir
. $VenvActivate

Write-Host "start TradingAgents..." -ForegroundColor Green

try {
    tradingagents
}
catch {
    Write-Host "tradingagents 命令失败，改用 python -m cli.main 启动..." -ForegroundColor Yellow
    python -m cli.main
}
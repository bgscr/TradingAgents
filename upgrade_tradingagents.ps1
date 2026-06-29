$ErrorActionPreference = "Stop"

$ProjectDir = "D:\prj\TradingAgents\TradingAgents"
$VenvActivate = Join-Path $ProjectDir ".venv\Scripts\Activate.ps1"

if (-not (Test-Path $ProjectDir)) {
    Write-Host "项目目录不存在: $ProjectDir" -ForegroundColor Red
    exit 1
}

if (-not (Test-Path (Join-Path $ProjectDir ".git"))) {
    Write-Host "当前目录不是 Git 仓库: $ProjectDir" -ForegroundColor Red
    exit 1
}

if (-not (Test-Path $VenvActivate)) {
    Write-Host "虚拟环境不存在: $VenvActivate" -ForegroundColor Red
    Write-Host "请先执行: python -m venv .venv" -ForegroundColor Yellow
    exit 1
}

Set-Location $ProjectDir

Write-Host "拉取最新代码..." -ForegroundColor Green
git pull

Write-Host "激活虚拟环境..." -ForegroundColor Green
. $VenvActivate

Write-Host "重新安装本地包..." -ForegroundColor Green
python -m pip install -e .

Write-Host "更新完成。" -ForegroundColor Green
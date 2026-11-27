[CmdletBinding(DefaultParameterSetName = 'Check')]
param(
    [string] $Ticker,
    [Parameter(ParameterSetName = 'Check')] [switch] $CheckOnly,
    [Parameter(Mandatory, ParameterSetName = 'Research')] [switch] $Run
)

$ErrorActionPreference = 'Stop'
function Assert-LocalPath([string] $Path) {
    $root = [IO.Path]::GetPathRoot($Path)
    if ($root -notmatch '^[A-Za-z]:[\\/]$' -or ([IO.DriveInfo]::new($root)).DriveType -ne 'Fixed') {
        throw '需要本机固定磁盘上的 Python 虚拟环境和项目。'
    }
    $cursor = $root
    foreach ($part in $Path.Substring($root.Length).Split([char[]]'\/')) {
        if ($part -eq '..') { throw '路径必须指向本机目录。' }
        if (-not $part) { continue }
        $cursor = Join-Path $cursor $part
        if (Test-Path -LiteralPath $cursor) {
            if ((Get-Item -Force -LiteralPath $cursor).Attributes -band [IO.FileAttributes]::ReparsePoint) {
                throw '当前入口要求直接使用本机目录。'
            }
        }
    }
}
if ([string]::IsNullOrWhiteSpace($Ticker)) {
    $Ticker = Read-Host '沪深 A 股股票代码'
}
$python = Join-Path $PSScriptRoot '.venv/Scripts/python.exe'
Assert-LocalPath $python
if (-not (Test-Path -LiteralPath $python -PathType Leaf)) {
    if (-not $env:VIRTUAL_ENV) {
        [Console]::Error.WriteLine('请先按安装说明创建或激活项目 Python 虚拟环境。')
        exit 2
    }
    $python = Join-Path $env:VIRTUAL_ENV 'Scripts/python.exe'
    Assert-LocalPath $python
}
$mode = if ($Run) { '--run' } else { '--check-only' }
$entry = Join-Path $PSScriptRoot 'scripts/personal_full.py'
Assert-LocalPath $entry
& $python -I -B $entry --ticker $Ticker $mode
exit $LASTEXITCODE

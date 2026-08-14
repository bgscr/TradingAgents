$ErrorActionPreference = "Stop"
Set-StrictMode -Version Latest

$controlPath = "C:\QCF17\Input\Control\run-control.json"
$runtimeContractPath = "C:\QCF17\Input\Runtime\runtime-bundle-contract.json"
$control = Get-Content -LiteralPath $controlPath -Raw | ConvertFrom-Json

Get-ChildItem Env: | Where-Object {
    $_.Name -match "(?i)(API_KEY|TOKEN|PASSWORD|SECRET|CREDENTIAL|COOKIE)"
} | ForEach-Object {
    [Environment]::SetEnvironmentVariable($_.Name, $null, "Process")
}

foreach ($entry in $control.environment.variables) {
    [Environment]::SetEnvironmentVariable($entry[0], $entry[1], "Process")
}

foreach ($guestPath in $control.guest_layout.writable_paths) {
    New-Item -ItemType Directory -Path $guestPath -Force | Out-Null
}

$runtimeContract = Get-Content -LiteralPath $runtimeContractPath -Raw | ConvertFrom-Json
if ($runtimeContract.state -ne "sealed") {
    throw "runtime_bundle_not_ready_for_iso_01"
}

$installer = Join-Path "C:\QCF17\Input\Runtime" $runtimeContract.bootstrap_relative_path
& $installer `
    -PythonTarget $control.guest_layout.python_install `
    -VirtualEnvironmentTarget $control.guest_layout.virtual_environment
if ($LASTEXITCODE -ne 0) {
    throw "runtime_bundle_bootstrap_failed"
}

$guestPython = Join-Path $control.guest_layout.virtual_environment "Scripts\python.exe"
$verificationOutput = Join-Path $control.guest_layout.guest_output "workspace-verification.json"
& $guestPython `
    "C:\QCF17\Input\Runner\verify_source.py" `
    --source-input "C:\QCF17\Input\Source" `
    --destination $control.guest_layout.workspace_repository `
    --verification-output $verificationOutput
if ($LASTEXITCODE -ne 0) {
    throw "reviewed_source_reconstruction_failed"
}

$guestGit = Join-Path "C:\QCF17\Input\Runtime" $runtimeContract.git_relative_path
& $guestGit -c core.hooksPath=NUL -C $control.guest_layout.workspace_repository read-tree HEAD
if ($LASTEXITCODE -ne 0) {
    throw "synthetic_git_index_failed"
}
$actualTree = & $guestGit -C $control.guest_layout.workspace_repository rev-parse "HEAD^{tree}"
if (($LASTEXITCODE -ne 0) -or ($actualTree.Trim() -ne $control.source.tree_hash)) {
    throw "reviewed_source_tree_mismatch"
}

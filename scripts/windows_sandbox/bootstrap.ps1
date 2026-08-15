$ErrorActionPreference = "Stop"
Set-StrictMode -Version Latest

$controlPath = "C:\QCF17\Input\Control\run-control.json"
$stage = "load_control"
$failed = $false
$executionMode = $null

function Set-BoundedFailureStage {
    param(
        [Parameter(Mandatory = $true)]
        [string]$Prefix,
        [Parameter(Mandatory = $true)]
        [string]$Reason
    )
    if ($Reason -match "^[a-z][a-z0-9_]{0,79}$") {
        $script:stage = $Prefix + ":" + $Reason
    }
}

try {
    $control = Get-Content -LiteralPath $controlPath -Raw | ConvertFrom-Json
    $executionMode = [string]$control.base.execution_mode
    if ($executionMode -notin @(
            "boundary_qualification",
            "ticket06_acceptance"
        )) {
        throw "unsupported_sandbox_execution_mode"
    }

    $stage = "purge_credentials"
    Get-ChildItem Env: | Where-Object {
        $_.Name -match "(?i)(API_KEY|TOKEN|PASSWORD|SECRET|CREDENTIAL|COOKIE)"
    } | ForEach-Object {
        [Environment]::SetEnvironmentVariable($_.Name, $null, "Process")
    }

    $stage = "apply_environment"
    foreach ($entry in $control.base.environment.variables) {
        [Environment]::SetEnvironmentVariable($entry[0], $entry[1], "Process")
    }

    $stage = "create_guest_layout"
    foreach ($property in $control.base.guest_layout.PSObject.Properties) {
        $guestPath = [string]$property.Value
        if (($property.Name -ne "workspace_repository") -and
            ($guestPath -match "(?i)^C:\\QCF17(?:\\|$)") -and
            ($guestPath -notmatch "(?i)^C:\\QCF17\\Input(?:\\|$)")) {
            New-Item -ItemType Directory -Path $guestPath -Force | Out-Null
        }
    }

    $stage = "validate_runtime_contract"
    $runtimeContract = $control.base.runtime_bundle
    if ([string]$runtimeContract.state -ne "sealed") {
        throw "runtime_bundle_not_ready_for_boundary_qualification"
    }

    $stage = "provision_runtime_offline"
    $installer = "C:\QCF17\Input\Runner\install_boundary_runtime.ps1"
    try {
        & $installer `
            -PythonTarget $control.base.guest_layout.python_install `
            -VirtualEnvironmentTarget $control.base.guest_layout.virtual_environment `
            -ControlPath $controlPath `
            -EvidencePath (Join-Path `
                $control.base.guest_layout.guest_output `
                "runtime-provisioning.json")
    }
    catch {
        Set-BoundedFailureStage `
            -Prefix "provision_runtime_offline" `
            -Reason ([string]$_.Exception.Message)
        throw
    }
    if ($LASTEXITCODE -ne 0) {
        throw "runtime_bundle_bootstrap_failed"
    }

    $guestPython = Join-Path `
        $control.base.guest_layout.virtual_environment `
        "Scripts\python.exe"
    $stage = "reconstruct_reviewed_source"
    $verificationOutput = Join-Path `
        $control.base.guest_layout.guest_output `
        "workspace-verification.json"
    & $guestPython `
        "C:\QCF17\Input\Runner\verify_source.py" `
        --source-input "C:\QCF17\Input\Source" `
        --destination $control.base.guest_layout.workspace_repository `
        --verification-output $verificationOutput
    if ($LASTEXITCODE -ne 0) {
        throw "reviewed_source_reconstruction_failed"
    }

    $stage = "verify_synthetic_git_tree"
    $guestGit = Join-Path `
        "C:\QCF17\Input\Runtime" `
        $runtimeContract.git_relative_path
    & $guestGit `
        -c core.hooksPath=NUL `
        -C $control.base.guest_layout.workspace_repository `
        read-tree HEAD
    if ($LASTEXITCODE -ne 0) {
        throw "synthetic_git_index_failed"
    }
    $actualTree = & $guestGit `
        -C $control.base.guest_layout.workspace_repository `
        rev-parse "HEAD^{tree}"
    if (($LASTEXITCODE -ne 0) -or `
        ($actualTree.Trim() -ne $control.base.source.tree_hash)) {
        throw "reviewed_source_tree_mismatch"
    }

    $stage = "install_project_offline"
    try {
        & $installer `
            -PythonTarget $control.base.guest_layout.python_install `
            -VirtualEnvironmentTarget $control.base.guest_layout.virtual_environment `
            -ProjectSource $control.base.guest_layout.workspace_repository `
            -ProjectOnly `
            -ControlPath $controlPath `
            -EvidencePath (Join-Path `
                $control.base.guest_layout.guest_output `
                "project-provisioning.json")
    }
    catch {
        Set-BoundedFailureStage `
            -Prefix "install_project_offline" `
            -Reason ([string]$_.Exception.Message)
        throw
    }
    if ($LASTEXITCODE -ne 0) {
        throw "project_offline_install_failed"
    }

    $stage = "reapply_environment"
    foreach ($entry in $control.base.environment.variables) {
        [Environment]::SetEnvironmentVariable($entry[0], $entry[1], "Process")
    }

    if ($executionMode -eq "boundary_qualification") {
        $stage = "execute_boundary_qualification"
        & $guestPython `
            "C:\QCF17\Input\Runner\run_boundary_qualification.py" `
            --control $controlPath `
            --source-input "C:\QCF17\Input\Source" `
            --runner-input "C:\QCF17\Input\Runner" `
            --runtime-input "C:\QCF17\Input\Runtime" `
            --results "C:\QCF17\Export" `
            --workspace $control.base.guest_layout.workspace_repository
        if ($LASTEXITCODE -ne 0) {
            $diagnosticPath = "C:\QCF17\Output\boundary-failure.json"
            try {
                if ((Test-Path -LiteralPath $diagnosticPath -PathType Leaf) -and
                    (Get-Item -LiteralPath $diagnosticPath).Length -le 4096) {
                    $diagnostic = Get-Content -LiteralPath $diagnosticPath -Raw |
                        ConvertFrom-Json
                    if ($diagnostic.contract_version -eq
                            "windows-sandbox-boundary-diagnostic-v1" -and
                        $diagnostic.status -eq "fail") {
                        Set-BoundedFailureStage `
                            -Prefix "execute_boundary_qualification" `
                            -Reason ([string]$diagnostic.reason)
                    }
                }
            }
            catch {
                # A missing or malformed diagnostic leaves the bounded outer stage.
            }
            throw "boundary_qualification_guest_failed"
        }
    }
    elseif ($executionMode -eq "ticket06_acceptance") {
        $stage = "execute_ticket06_acceptance"
        & $guestPython `
            "C:\QCF17\Input\Runner\run_ticket06_acceptance.py" `
            --control $controlPath `
            --runner-input "C:\QCF17\Input\Runner" `
            --results "C:\QCF17\Export" `
            --workspace $control.base.guest_layout.workspace_repository `
            --git-executable "C:\QCF17\Input\Runtime\git\cmd\git.exe"
        if ($LASTEXITCODE -ne 0) {
            throw "ticket06_acceptance_guest_failed"
        }
    }
    $stage = "completed"
}
catch {
    $failed = $true
    if ($executionMode -ne "ticket06_acceptance") {
      try {
        $material = [Text.Encoding]::UTF8.GetBytes(
            $stage + ":" + [string]$_.FullyQualifiedErrorId
        )
        $hasher = [Security.Cryptography.SHA256]::Create()
        try {
            $digestBytes = $hasher.ComputeHash($material)
        }
        finally {
            $hasher.Dispose()
        }
        $digest = -join ($digestBytes | ForEach-Object { $_.ToString("x2") })
        $failure = [ordered]@{
            contract_version = "windows-sandbox-boundary-failure-v1"
            failure_identity = "guest-failure:sha256:" + $digest
            stage = $stage
            status = "fail"
        } | ConvertTo-Json -Compress
        [IO.File]::WriteAllText(
            "C:\QCF17\Export\failure.json",
            $failure,
            [Text.UTF8Encoding]::new($false)
        )
      }
      catch {
          # Missing/malformed bounded output is itself a fail-closed host result.
      }
    }
}
finally {
    shutdown.exe /s /t 0 /f | Out-Null
}

if ($failed) {
    exit 1
}
exit 0

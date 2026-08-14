param(
    [Parameter(Mandatory = $true)]
    [string]$PythonTarget,

    [Parameter(Mandatory = $true)]
    [string]$VirtualEnvironmentTarget,

    [string]$ProjectSource,

    [switch]$ProjectOnly,

    [string]$ControlPath = "C:\QCF17\Input\Control\run-control.json",

    [string]$EvidencePath = "C:\QCF17\Output\runtime-provisioning.json"
)

$ErrorActionPreference = "Stop"
Set-StrictMode -Version Latest

$bundleRoot = "C:\QCF17\Input\Runtime"
$manifestPath = Join-Path $bundleRoot "bundle-manifest.json"

function ConvertTo-CanonicalJsonString {
    param([string]$Value)

    $builder = [Text.StringBuilder]::new($Value.Length + 2)
    [void]$builder.Append('"')
    foreach ($character in $Value.ToCharArray()) {
        $code = [int]$character
        switch ($code) {
            8 { [void]$builder.Append('\b'); continue }
            9 { [void]$builder.Append('\t'); continue }
            10 { [void]$builder.Append('\n'); continue }
            12 { [void]$builder.Append('\f'); continue }
            13 { [void]$builder.Append('\r'); continue }
            34 { [void]$builder.Append('\"'); continue }
            92 { [void]$builder.Append('\\'); continue }
        }
        if ($code -lt 32) {
            [void]$builder.Append(('\u{0:x4}' -f $code))
        }
        else {
            [void]$builder.Append($character)
        }
    }
    [void]$builder.Append('"')
    return $builder.ToString()
}

function ConvertTo-CanonicalJson {
    param([AllowNull()]$Value)

    if ($null -eq $Value) { return "null" }
    if ($Value -is [string]) { return ConvertTo-CanonicalJsonString $Value }
    if ($Value -is [bool]) { return $(if ($Value) { "true" } else { "false" }) }
    if ($Value -is [byte] -or $Value -is [sbyte] -or
        $Value -is [int16] -or $Value -is [uint16] -or
        $Value -is [int32] -or $Value -is [uint32] -or
        $Value -is [int64] -or $Value -is [uint64]) {
        return $Value.ToString([Globalization.CultureInfo]::InvariantCulture)
    }
    if ($Value -is [System.Collections.IDictionary]) {
        $items = foreach ($key in @($Value.Keys | Sort-Object)) {
            (ConvertTo-CanonicalJson ([string]$key)) + ":" +
                (ConvertTo-CanonicalJson $Value[$key])
        }
        return "{" + ($items -join ",") + "}"
    }
    if ($Value -is [pscustomobject]) {
        $dictionary = @{}
        foreach ($property in $Value.PSObject.Properties) {
            $dictionary[$property.Name] = $property.Value
        }
        return ConvertTo-CanonicalJson $dictionary
    }
    if ($Value -is [System.Collections.IEnumerable]) {
        $items = foreach ($item in $Value) { ConvertTo-CanonicalJson $item }
        return "[" + ($items -join ",") + "]"
    }
    throw "bundle_manifest_value_invalid"
}

function Get-Sha256Hex {
    param([byte[]]$Bytes)
    $algorithm = [Security.Cryptography.SHA256]::Create()
    try {
        return ([BitConverter]::ToString($algorithm.ComputeHash($Bytes))).Replace("-", "").ToLowerInvariant()
    }
    finally {
        $algorithm.Dispose()
    }
}

function Get-ContentIdentity {
    param(
        [string]$Namespace,
        [AllowNull()]$Value
    )
    $canonical = ConvertTo-CanonicalJson $Value
    return $Namespace + ":sha256:" +
        (Get-Sha256Hex ([Text.Encoding]::UTF8.GetBytes($canonical)))
}

function Fail-Bundle {
    param([string]$Reason)
    throw $Reason
}

function Fail-Native {
    param(
        [string]$Reason,
        [int]$ExitCode
    )
    $boundedExitCode = ([int64]$ExitCode).ToString(
        [Globalization.CultureInfo]::InvariantCulture
    ).Replace("-", "neg")
    Fail-Bundle ($Reason + "_" + $boundedExitCode)
}

function Invoke-NativeClosed {
    param(
        [Parameter(Mandatory = $true)]
        [string]$Executable,
        [Parameter(Mandatory = $true)]
        [string[]]$Arguments
    )

    if (-not (Test-Path -LiteralPath $Executable -PathType Leaf)) {
        return -1
    }
    $previousPreference = $ErrorActionPreference
    $exitCode = $null
    try {
        # Windows PowerShell 5.1 promotes native stderr to NativeCommandError
        # when the script-wide preference is Stop.  Exit status, not stderr
        # formatting, is the reviewed authority for these pinned executables.
        $ErrorActionPreference = "Continue"
        $global:LASTEXITCODE = $null
        & $Executable @Arguments *> $null
        $exitCode = $LASTEXITCODE
    }
    catch {
        $exitCode = -1
    }
    finally {
        $ErrorActionPreference = $previousPreference
    }
    if ($null -eq $exitCode) { return -1 }
    return [int]$exitCode
}

function Invoke-InstallerClosed {
    param(
        [Parameter(Mandatory = $true)]
        [string]$Executable,
        [Parameter(Mandatory = $true)]
        [string[]]$Arguments
    )

    if (-not (Test-Path -LiteralPath $Executable -PathType Leaf)) {
        return -1
    }
    try {
        # The CPython bootstrapper is a GUI-subsystem executable.  Start-Process
        # is required for a synchronous, authoritative exit code under Windows
        # PowerShell 5.1; the call operator can return before it completes.
        $process = Start-Process `
            -FilePath $Executable `
            -ArgumentList $Arguments `
            -WindowStyle Hidden `
            -Wait `
            -PassThru
        return [int]$process.ExitCode
    }
    catch {
        return -1
    }
}

function Assert-PinnedPath {
    param(
        [string]$Actual,
        [string]$Expected
    )
    try {
        $actualCanonical = [IO.Path]::GetFullPath($Actual).TrimEnd("\")
        $expectedCanonical = [IO.Path]::GetFullPath($Expected).TrimEnd("\")
    }
    catch {
        Fail-Bundle "guest_provisioning_path_invalid"
    }
    if (-not $actualCanonical.Equals(
            $expectedCanonical,
            [StringComparison]::OrdinalIgnoreCase
        )) {
        Fail-Bundle "guest_provisioning_path_invalid"
    }
}

function Assert-NoReparseComponent {
    param([string]$Path)
    $current = [IO.Path]::GetFullPath($Path)
    while (-not [string]::IsNullOrEmpty($current)) {
        if (Test-Path -LiteralPath $current) {
            $item = Get-Item -LiteralPath $current -Force
            if (($item.Attributes -band [IO.FileAttributes]::ReparsePoint) -ne 0) {
                Fail-Bundle "guest_provisioning_reparse_point"
            }
        }
        $parent = [IO.Directory]::GetParent($current)
        if ($null -eq $parent) { break }
        $current = $parent.FullName
    }
}

Assert-PinnedPath $PythonTarget "C:\QCF17\Python"
Assert-PinnedPath $VirtualEnvironmentTarget "C:\QCF17\Venv"
Assert-PinnedPath $ControlPath "C:\QCF17\Input\Control\run-control.json"
if ($ProjectOnly) {
    Assert-PinnedPath $ProjectSource "C:\QCF17\Work\Repository"
    Assert-PinnedPath $EvidencePath "C:\QCF17\Output\project-provisioning.json"
    if (-not (Test-Path -LiteralPath "C:\QCF17\Venv\Scripts\python.exe" -PathType Leaf)) {
        Fail-Bundle "verified_virtual_environment_required"
    }
}
else {
    if (-not [string]::IsNullOrWhiteSpace($ProjectSource)) {
        Fail-Bundle "project_source_not_permitted"
    }
    Assert-PinnedPath $EvidencePath "C:\QCF17\Output\runtime-provisioning.json"
}
foreach ($path in @(
        $PythonTarget,
        $VirtualEnvironmentTarget,
        $ControlPath,
        $EvidencePath,
        $(if ($ProjectOnly) { $ProjectSource } else { $bundleRoot })
    )) {
    Assert-NoReparseComponent $path
}

Get-ChildItem Env: | Where-Object {
    $_.Name -match "^(?i:PIP_|HTTP_PROXY$|HTTPS_PROXY$|ALL_PROXY$|NO_PROXY$)"
} | Remove-Item
$env:PIP_CONFIG_FILE = "NUL"
$env:PIP_DISABLE_PIP_VERSION_CHECK = "1"
$env:PIP_NO_INDEX = "1"
$env:PYTHONNOUSERSITE = "1"

$controlBytes = [IO.File]::ReadAllBytes($ControlPath)
$manifestBytes = [IO.File]::ReadAllBytes($manifestPath)
$control = [Text.Encoding]::UTF8.GetString($controlBytes) | ConvertFrom-Json
$manifest = [Text.Encoding]::UTF8.GetString($manifestBytes) | ConvertFrom-Json
$runtimeContract = $control.runtime_bundle

if ($runtimeContract.state -ne "sealed" -or
    -not $runtimeContract.mapped_read_only -or
    -not $runtimeContract.offline_required -or
    $runtimeContract.network_install_permitted) {
    Fail-Bundle "runtime_bundle_contract_invalid"
}
if ((Get-Sha256Hex $manifestBytes) -ne $runtimeContract.bundle_manifest_sha256) {
    Fail-Bundle "bundle_manifest_hash_mismatch"
}
if ($manifest.bundle_identity -ne $runtimeContract.bundle_manifest_identity) {
    Fail-Bundle "bundle_manifest_identity_mismatch"
}

# Windows PowerShell 5.1 does not preserve the Python canonical-JSON model for
# every nested manifest projection.  The exact manifest bytes are already
# SHA-256 pinned by the read-only control above.  After the pinned installer is
# installed, bootstrap/verify_bundle.py recomputes the canonical bundle identity
# under isolated CPython before any dependency or project installation occurs.
if ($manifest.contract_version -ne "windows-offline-runtime-bundle-v1" -or
    $manifest.supported_windows_architecture -ne "AMD64" -or
    $manifest.python_implementation -ne "CPython" -or
    $manifest.python_version -ne "3.13.7" -or
    $manifest.python_tag -ne "cp313" -or
    $manifest.abi_tag -ne "cp313" -or
    $manifest.platform_tag -ne "win_amd64" -or
    $manifest.python_installer_relative_path -ne "python/python-3.13.7-amd64.exe" -or
    $manifest.dependency_lock_relative_path -ne "locks/requirements-win-cp313.txt" -or
    $manifest.bootstrap_relative_path -ne "bootstrap/install-runtime.ps1" -or
    $manifest.verifier_relative_path -ne "bootstrap/verify_bundle.py" -or
    -not $manifest.portable_git_required -or
    $manifest.portable_git_relative_path -ne "git/cmd/git.exe" -or
    $manifest.reviewed_source_manifest_relative_path -ne
        "provenance/source-manifest.json" -or
    $manifest.runtime_artifact_lock_relative_path -ne
        "provenance/iso02-artifact-lock.json" -or
    $manifest.project_metadata_relative_path -ne "provenance/pyproject.toml" -or
    $manifest.portable_git_archive_relative_path -ne
        "provenance/MinGit-2.45.1-64-bit.zip" -or
    $manifest.iso01_runtime_bundle_input_contract_version -ne
        "windows-sandbox-runtime-bundle-input-v1") {
    Fail-Bundle "bundle_manifest_schema_mismatch"
}
if ($manifest.files.Count -lt 5 -or $manifest.files.Count -gt 65536 -or
    $manifest.wheelhouse_inventory.Count -lt 1 -or
    $manifest.wheelhouse_inventory.Count -gt 2048 -or
    $manifest.dependency_roots.Count -lt 1 -or $manifest.dependency_roots.Count -gt 512) {
    Fail-Bundle "bundle_manifest_unbounded"
}

$sourceFields = @(
    "contract_version",
    "commit_id",
    "tree_hash",
    "source_input_identity",
    "source_manifest_identity",
    "source_archive_sha256"
)
foreach ($field in $sourceFields) {
    if ([string]$manifest.source.$field -ne [string]$control.source.$field) {
        Fail-Bundle "source_bundle_binding_mismatch"
    }
}

$expected = @{}
foreach ($entry in $manifest.files) {
    $relative = ([string]$entry.relative_path).Replace("\", "/")
    if ($relative.StartsWith("/") -or $relative.Contains("../") -or
        $relative.Contains("/../") -or $expected.ContainsKey($relative.ToLowerInvariant())) {
        Fail-Bundle "bundle_manifest_path_invalid"
    }
    if ($relative -match "(?i)(^|/)(\.env|\.env\.enterprise|pip\.ini)$" -or
        $relative -match "(?i)\.(db|sqlite|sqlite3)(-wal|-shm)?$") {
        Fail-Bundle "forbidden_bundle_input"
    }
    $artifactType = [IO.Path]::GetExtension($relative).TrimStart(".").ToLowerInvariant()
    if ([string]::IsNullOrEmpty($artifactType)) { $artifactType = "file" }
    if ($artifactType -ne [string]$entry.artifact_type) {
        Fail-Bundle "bundle_manifest_role_mismatch"
    }
    $expected[$relative.ToLowerInvariant()] = $entry
}

$requiredRoles = @{
    "bootstrap/install-runtime.ps1" = "bootstrap_script"
    "bootstrap/verify_bundle.py" = "verifier_script"
    "git/cmd/git.exe" = "portable_git_executable"
    "locks/requirements-win-cp313.txt" = "dependency_lock"
    "python/python-3.13.7-amd64.exe" = "python_installer"
    "provenance/mingit-2.45.1-64-bit.zip" = "portable_git_archive"
    "provenance/iso02-artifact-lock.json" = "runtime_artifact_lock"
    "provenance/pyproject.toml" = "project_metadata"
    "provenance/source-manifest.json" = "reviewed_source_manifest"
}
foreach ($key in $expected.Keys) {
    $role = [string]$expected[$key].role
    if ($requiredRoles.ContainsKey($key)) {
        if ($role -ne $requiredRoles[$key]) { Fail-Bundle "bundle_manifest_role_mismatch" }
    }
    elseif ($key.StartsWith("wheelhouse/")) {
        if ($role -ne "dependency_wheel") { Fail-Bundle "bundle_manifest_role_mismatch" }
    }
    elseif ($key.StartsWith("git/")) {
        if ($role -ne "portable_git_support") { Fail-Bundle "bundle_manifest_role_mismatch" }
    }
    else {
        Fail-Bundle "bundle_manifest_role_mismatch"
    }
}
foreach ($key in $requiredRoles.Keys) {
    if (-not $expected.ContainsKey($key)) { Fail-Bundle "bundle_manifest_role_mismatch" }
}
$inventoryDistributions = @{}
foreach ($wheel in $manifest.wheelhouse_inventory) {
    $distribution = ([string]$wheel.distribution).ToLowerInvariant()
    $wheelKey = "wheelhouse/" + ([string]$wheel.filename).ToLowerInvariant()
    if ($inventoryDistributions.ContainsKey($distribution) -or
        -not $expected.ContainsKey($wheelKey) -or
        [string]$expected[$wheelKey].role -ne "dependency_wheel" -or
        [int64]$expected[$wheelKey].byte_length -ne [int64]$wheel.byte_length -or
        [string]$expected[$wheelKey].sha256 -ne [string]$wheel.sha256) {
        Fail-Bundle "wheel_manifest_disagreement"
    }
    $inventoryDistributions[$distribution] = $true
}
$wheelFileCount = @($manifest.files | Where-Object {
    $_.role -eq "dependency_wheel"
}).Count
if ($wheelFileCount -ne $manifest.wheelhouse_inventory.Count) {
    Fail-Bundle "wheel_manifest_disagreement"
}
if ((Get-ContentIdentity "windows-offline-python-runtime:v1" `
        $expected["python/python-3.13.7-amd64.exe"]) -ne
    $manifest.python_runtime_identity) {
    Fail-Bundle "python_runtime_identity_mismatch"
}
if ((Get-ContentIdentity "windows-offline-dependency-lock:v1" `
        $expected["locks/requirements-win-cp313.txt"]) -ne
    $manifest.dependency_lock_identity) {
    Fail-Bundle "dependency_lock_identity_mismatch"
}
# The complete wheel inventory remains structurally checked above and every
# wheel is length/hash checked below.  Its canonical identity is revalidated by
# the isolated Python verifier; PS 5.1 cannot reproduce that array projection.
$supportEntries = @($manifest.files | Where-Object {
    $_.role -eq "bootstrap_script" -or $_.role -eq "verifier_script"
})
if ((Get-ContentIdentity "windows-offline-bootstrap-support:v1" $supportEntries) -ne
    $manifest.bootstrap_support_identity) {
    Fail-Bundle "bootstrap_support_identity_mismatch"
}
$gitEntries = @($manifest.files | Where-Object {
    $_.role -eq "portable_git_executable" -or $_.role -eq "portable_git_support"
})
if ((Get-ContentIdentity "windows-offline-portable-git:v1" $gitEntries) -ne
    $manifest.portable_git_identity) {
    Fail-Bundle "portable_git_identity_mismatch"
}
$expectedDependencyArguments = @(
    "-m", "pip", "--isolated", "install", "--no-index", "--require-hashes", "--find-links",
    "C:\QCF17\Input\Runtime\wheelhouse", "-r",
    "C:\QCF17\Input\Runtime\locks\requirements-win-cp313.txt"
)
$expectedProjectArguments = @(
    "-m", "pip", "--isolated", "install", "--no-index", "--no-deps", "--no-build-isolation",
    "C:\QCF17\Work\Repository"
)
if ((ConvertTo-CanonicalJson @($manifest.install_policy.dependency_install_arguments)) -ne
        (ConvertTo-CanonicalJson $expectedDependencyArguments) -or
    (ConvertTo-CanonicalJson @($manifest.install_policy.project_install_arguments)) -ne
        (ConvertTo-CanonicalJson $expectedProjectArguments) -or
    $manifest.install_policy.network_permitted -or
    $manifest.install_policy.host_python_permitted -or
    $manifest.install_policy.host_virtual_environment_permitted) {
    Fail-Bundle "offline_install_policy_mismatch"
}

$actual = @{}
foreach ($file in Get-ChildItem -LiteralPath $bundleRoot -Recurse -File) {
    if (($file.Attributes -band [IO.FileAttributes]::ReparsePoint) -ne 0) {
        Fail-Bundle "bundle_reparse_point"
    }
    $relative = $file.FullName.Substring($bundleRoot.Length).TrimStart("\").Replace("\", "/")
    if ($relative -eq "bundle-manifest.json") { continue }
    $key = $relative.ToLowerInvariant()
    if ($actual.ContainsKey($key) -or -not $expected.ContainsKey($key)) {
        Fail-Bundle "bundle_membership_mismatch"
    }
    $actual[$key] = $true
    $entry = $expected[$key]
    if ($file.Length -ne [int64]$entry.byte_length) {
        Fail-Bundle "bundle_file_length_mismatch"
    }
    if ((Get-FileHash -LiteralPath $file.FullName -Algorithm SHA256).Hash.ToLowerInvariant() -ne
        [string]$entry.sha256) {
        Fail-Bundle "bundle_file_hash_mismatch"
    }
}
if ($actual.Count -ne $expected.Count) {
    Fail-Bundle "bundle_membership_mismatch"
}

$reviewedInputKeys = @(
    "provenance/source-manifest.json",
    "provenance/iso02-artifact-lock.json",
    "provenance/pyproject.toml",
    "provenance/mingit-2.45.1-64-bit.zip"
)
$reviewedInputs = @($reviewedInputKeys | ForEach-Object { $expected[$_] })
if ((Get-ContentIdentity "windows-offline-reviewed-inputs:v1" $reviewedInputs) -ne
    $manifest.reviewed_input_identity) {
    Fail-Bundle "reviewed_input_identity_mismatch"
}

$sourceManifestEntry = $expected["provenance/source-manifest.json"]
if ([int64]$sourceManifestEntry.byte_length -gt 8388608) {
    Fail-Bundle "source_manifest_invalid"
}
$sourceManifestPath = Join-Path $bundleRoot "provenance/source-manifest.json"
$sourceManifest = [Text.Encoding]::UTF8.GetString(
    [IO.File]::ReadAllBytes($sourceManifestPath)
) | ConvertFrom-Json
$sourceManifestPayload = @{}
foreach ($property in $sourceManifest.PSObject.Properties) {
    if ($property.Name -ne "source_manifest_identity") {
        $sourceManifestPayload[$property.Name] = $property.Value
    }
}
if ($sourceManifest.contract_version -ne $manifest.source.contract_version -or
    $sourceManifest.commit_id -ne $manifest.source.commit_id -or
    $sourceManifest.tree_hash -ne $manifest.source.tree_hash -or
    $sourceManifest.source_manifest_identity -ne
        $manifest.source.source_manifest_identity -or
    (Get-ContentIdentity "windows-sandbox-source-manifest:v1" `
        $sourceManifestPayload) -ne $sourceManifest.source_manifest_identity -or
    $sourceManifest.entries.Count -lt 1 -or
    $sourceManifest.entries.Count -gt 65536) {
    Fail-Bundle "source_manifest_identity_mismatch"
}
$sourceEntries = @{}
foreach ($entry in $sourceManifest.entries) {
    $relative = ([string]$entry.relative_path).Replace("\", "/")
    $key = $relative.ToLowerInvariant()
    if ($relative.StartsWith("/") -or $relative.Contains("../") -or
        $relative.Contains("/../") -or $sourceEntries.ContainsKey($key) -or
        [string]$entry.sha256 -notmatch "^[0-9a-f]{64}$" -or
        [int64]$entry.byte_length -lt 0) {
        Fail-Bundle "source_manifest_invalid"
    }
    $sourceEntries[$key] = $entry
}
$trackedBindings = @{
    "provenance/iso02-artifact-lock.json" =
        "scripts/windows_sandbox/iso02-artifact-lock.json"
    "provenance/pyproject.toml" = "pyproject.toml"
    "bootstrap/install-runtime.ps1" =
        "scripts/windows_sandbox/offline_bundle_bootstrap.ps1"
    "bootstrap/verify_bundle.py" =
        "scripts/windows_sandbox/verify_offline_bundle.py"
    "locks/requirements-win-cp313.txt" =
        "scripts/windows_sandbox/iso02-requirements-win-cp313.txt"
}
foreach ($bundleKey in $trackedBindings.Keys) {
    $sourceKey = $trackedBindings[$bundleKey].ToLowerInvariant()
    if (-not $sourceEntries.ContainsKey($sourceKey) -or
        [int64]$sourceEntries[$sourceKey].byte_length -ne
            [int64]$expected[$bundleKey].byte_length -or
        [string]$sourceEntries[$sourceKey].sha256 -ne
            [string]$expected[$bundleKey].sha256) {
        Fail-Bundle "reviewed_source_binding_mismatch"
    }
}
if ([string]$expected["provenance/pyproject.toml"].sha256 -ne
    [string]$manifest.project_metadata_sha256) {
    Fail-Bundle "project_metadata_mismatch"
}

$artifactLockEntry = $expected["provenance/iso02-artifact-lock.json"]
if ([int64]$artifactLockEntry.byte_length -gt 65536) {
    Fail-Bundle "artifact_lock_invalid"
}
$artifactLockPath = Join-Path $bundleRoot "provenance/iso02-artifact-lock.json"
$artifactLock = [Text.Encoding]::UTF8.GetString(
    [IO.File]::ReadAllBytes($artifactLockPath)
) | ConvertFrom-Json
if ($artifactLock.contract_version -ne
        "windows-offline-runtime-artifact-lock-v1" -or
    $artifactLock.python.architecture -ne "AMD64" -or
    $artifactLock.python.implementation -ne "CPython" -or
    $artifactLock.python.version -ne "3.13.7" -or
    $artifactLock.python.artifact_filename -ne "python-3.13.7-amd64.exe" -or
    -not $artifactLock.portable_git.required -or
    $artifactLock.portable_git.archive_filename -ne "MinGit-2.45.1-64-bit.zip") {
    Fail-Bundle "artifact_lock_invalid"
}
$pythonEntry = $expected["python/python-3.13.7-amd64.exe"]
if ([int64]$pythonEntry.byte_length -ne
        [int64]$artifactLock.python.artifact_byte_length -or
    [string]$pythonEntry.sha256 -ne
        [string]$artifactLock.python.artifact_sha256) {
    Fail-Bundle "python_artifact_lock_mismatch"
}
$portableGitArchiveEntry = $expected["provenance/mingit-2.45.1-64-bit.zip"]
if ([int64]$portableGitArchiveEntry.byte_length -ne
        [int64]$artifactLock.portable_git.archive_byte_length -or
    [string]$portableGitArchiveEntry.sha256 -ne
        [string]$artifactLock.portable_git.archive_sha256) {
    Fail-Bundle "portable_git_artifact_lock_mismatch"
}

$pythonInstallerInput = Join-Path $bundleRoot $manifest.python_installer_relative_path
$installedPython = Join-Path $PythonTarget "python.exe"
$venvPython = Join-Path $VirtualEnvironmentTarget "Scripts\python.exe"
$wheelhouse = Join-Path $bundleRoot "wheelhouse"
$requirements = Join-Path $bundleRoot $manifest.dependency_lock_relative_path
$verifier = Join-Path $bundleRoot $manifest.verifier_relative_path

if (-not $ProjectOnly) {
    $installerStagingRoot = "C:\QCF17\Runtime\Installer"
    New-Item -ItemType Directory -Path $installerStagingRoot -Force | Out-Null
    Assert-NoReparseComponent $installerStagingRoot
    $pythonInstaller = Join-Path $installerStagingRoot "python-3.13.7-amd64.exe"
    [IO.File]::Copy($pythonInstallerInput, $pythonInstaller, $false)
    if ((Get-Item -LiteralPath $pythonInstaller).Length -ne
            [int64]$pythonEntry.byte_length -or
        (Get-FileHash -LiteralPath $pythonInstaller -Algorithm SHA256).Hash.ToLowerInvariant() -ne
            [string]$pythonEntry.sha256) {
        Fail-Bundle "guest_python_installer_copy_mismatch"
    }
    New-Item -ItemType Directory -Path $PythonTarget -Force | Out-Null
    $exitCode = Invoke-InstallerClosed $pythonInstaller @(
        "/quiet", "InstallAllUsers=0", "TargetDir=$PythonTarget", "Include_launcher=0",
        "Include_pip=1", "Include_test=0", "PrependPath=0", "Shortcuts=0"
    )
    if ($exitCode -ne 0) { Fail-Native "python_runtime_install_failed" $exitCode }

    $exitCode = Invoke-NativeClosed $installedPython @(
        "-I", "-m", "venv", "--clear", $VirtualEnvironmentTarget
    )
    if ($exitCode -ne 0) { Fail-Native "virtual_environment_creation_failed" $exitCode }

    $exitCode = Invoke-NativeClosed $installedPython @(
        "-I", $verifier,
        "--bundle-root", $bundleRoot,
        "--expected-bundle-identity", [string]$manifest.bundle_identity,
        "--expected-source-input-identity", [string]$manifest.source.source_input_identity
    )
    if ($exitCode -ne 0) { Fail-Native "bundle_python_verification_failed" $exitCode }

    $exitCode = Invoke-NativeClosed $venvPython @(
        "-I", "-m", "pip", "--isolated", "install", "--dry-run", "--no-index",
        "--require-hashes", "--find-links", $wheelhouse, "-r", $requirements
    )
    if ($exitCode -ne 0) { Fail-Native "offline_dependency_closure_failed" $exitCode }

    $exitCode = Invoke-NativeClosed $venvPython @(
        "-I", "-m", "pip", "--isolated", "install", "--no-index",
        "--require-hashes", "--find-links", $wheelhouse, "-r", $requirements
    )
    if ($exitCode -ne 0) { Fail-Native "offline_dependency_install_failed" $exitCode }
    $stage = "runtime_and_dependencies"
}
else {
    if ([string]::IsNullOrWhiteSpace($ProjectSource)) {
        Fail-Bundle "project_source_required"
    }
    $exitCode = Invoke-NativeClosed $venvPython @(
        "-I", "-m", "pip", "--isolated", "install", "--no-index", "--no-deps",
        "--no-build-isolation", $ProjectSource
    )
    if ($exitCode -ne 0) { Fail-Native "offline_project_install_failed" $exitCode }
    $stage = "project"
}

$evidence = [ordered]@{
    bundle_identity = $manifest.bundle_identity
    contract_version = "windows-offline-provisioning-evidence-v1"
    host_python_used = $false
    network_used = $false
    source_input_identity = $manifest.source.source_input_identity
    stage = $stage
    status = "provisioned"
}
$evidenceJson = $evidence | ConvertTo-Json -Compress
if ([Text.Encoding]::UTF8.GetByteCount($evidenceJson) -gt 4096) {
    Fail-Bundle "provisioning_evidence_unbounded"
}
$evidenceDirectory = Split-Path -Parent $EvidencePath
New-Item -ItemType Directory -Path $evidenceDirectory -Force | Out-Null
[IO.File]::WriteAllText($EvidencePath, $evidenceJson + "`n", [Text.UTF8Encoding]::new($false))

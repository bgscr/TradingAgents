# Invoke only the namespace initializer from the exact registered consumer
# assembly. This does not provision or enroll an owner or initialize a ledger.
[CmdletBinding(DefaultParameterSetName='Initialize')]
param(
    [Parameter(Mandatory=$true)][string] $Instance,
    [Parameter(Mandatory=$true)][string] $DeploymentPath,
    [Parameter(Mandatory=$true)][string] $DeploymentSha256,
    [Parameter(Mandatory=$true)][string] $ConsumerPath,
    [Parameter(Mandatory=$true)][string] $ConsumerSha256,
    [Parameter(Mandatory=$true)][string] $PinLibraryPath,
    [Parameter(Mandatory=$true)][string] $PinLibrarySha256,
    [Parameter(Mandatory=$true,ParameterSetName='Initialize')][string] $RequestPath,
    [Parameter(Mandatory=$true,ParameterSetName='Initialize')][string] $RequestSha256,
    [Parameter(Mandatory=$true,ParameterSetName='ValidateOnly')][switch] $ValidateOnly,
    [switch] $StagedFixture
)
$ErrorActionPreference='Stop'
if ($PSVersionTable.PSEdition -ne 'Desktop' -or $PSVersionTable.PSVersion.Major -ne 5 -or $PSVersionTable.PSVersion.Minor -ne 1 -or [IntPtr]::Size -ne 8) {
    throw 'The registered .NET Framework assembly requires 64-bit Windows PowerShell 5.1.'
}
if ($Instance -cnotmatch '\Atest-[0-9]{8}-[0-9]{3}\z') {
    throw 'Namespace initialization requires the exact selected TEST instance.'
}
[void][Reflection.Assembly]::LoadWithPartialName('System.Web.Extensions')
$json=[Web.Script.Serialization.JavaScriptSerializer]::new()
$json.MaxJsonLength=67108864
function Read-ExactBinding([string]$path,[string]$digest,[long]$length=-1) {
    if ($digest -cnotmatch '\A[0-9a-f]{64}\z' -or $path -cnotmatch '\A[A-Z]:\\' -or $path -cne [IO.Path]::GetFullPath($path)) {throw 'Exact absolute local binding path and hash required.'}
    $raw=[IO.File]::ReadAllBytes($path)
    if ($length -ge 0 -and $raw.Length -ne $length) {throw 'Reviewed binding length changed.'}
    $hash=[Security.Cryptography.SHA256]::Create()
    try {$actual=[BitConverter]::ToString($hash.ComputeHash($raw)).Replace('-','').ToLowerInvariant()} finally {$hash.Dispose()}
    if ($actual -cne $digest) {throw 'Reviewed deployment binding bytes changed.'}
    return $json.DeserializeObject([Text.UTF8Encoding]::new($false,$true).GetString($raw))
}
$deployment=Read-ExactBinding $DeploymentPath $DeploymentSha256
$registeredRoot='C:\ProgramData\TA-PolicyOwner-'+$Instance
if ($deployment.contract_version -cne 'registered-test-deployment-v1' -or $deployment.instance -cne $Instance -or $deployment.owner_root -cne $registeredRoot -or $deployment.operational -ne $false -or $deployment.execution_authorized -ne $false) {throw 'Deployment differs from the exact selected TEST instance.'}
$setup=Read-ExactBinding $deployment.setup_proposal.path $deployment.setup_proposal.sha256 ([long]$deployment.setup_proposal.byte_length)
if ($setup.contract_version -cne 'policy-owner-setup-proposal-v1' -or $setup.instance -cne $Instance -or $setup.root -cne $registeredRoot -or $setup.execution_authorized -ne $false -or $setup.registration_is_approval -ne $false) {throw 'Setup differs from the reviewed TEST deployment.'}
$bundle=[IO.Path]::GetDirectoryName($deployment.setup_proposal.path)
$location=if($StagedFixture){[IO.Path]::Combine($bundle,'depot')}else{$registeredRoot}
$members=@{}
foreach($row in $setup.files) {
    if ($members.ContainsKey($row.relative_path)) {throw 'Duplicate reviewed setup member.'}
    $members.Add($row.relative_path,$row)
}
foreach($required in @('bin/ConditionalRuntimeProtectionHost.exe','source/scripts/policy_owner/pinned_setup_native.ps1','source/scripts/policy_owner/initialize_test_namespace.ps1','source/scripts/runtime_protection/RuntimeProtectionHost.cs')) {
    if (-not $members.ContainsKey($required)) {throw ('Missing reviewed namespace input: '+$required)}
}
$expectedConsumerPath=[IO.Path]::Combine($location,'bin\ConditionalRuntimeProtectionHost.exe')
$expectedLibraryPath=[IO.Path]::Combine($location,'source\scripts\policy_owner\pinned_setup_native.ps1')
$expectedHelperPath=[IO.Path]::Combine($location,'source\scripts\policy_owner\initialize_test_namespace.ps1')
if ($ConsumerPath -cne $expectedConsumerPath) {
    throw 'Consumer path differs from the exact selected TEST instance.'
}
if ($PinLibraryPath -cne $expectedLibraryPath -or $PSCommandPath -cne $expectedHelperPath) {throw 'Library/helper path differs from the exact reviewed deployment.'}
if ($ConsumerSha256 -cne $members['bin/ConditionalRuntimeProtectionHost.exe'].sha256 -or $PinLibrarySha256 -cne $members['source/scripts/policy_owner/pinned_setup_native.ps1'].sha256) {throw 'Caller pins differ from the reviewed setup.'}
$helperBytes=[IO.File]::ReadAllBytes($PSCommandPath)
$hash=[Security.Cryptography.SHA256]::Create()
try {$helperHash=[BitConverter]::ToString($hash.ComputeHash($helperBytes)).Replace('-','').ToLowerInvariant()} finally {$hash.Dispose()}
if ($helperHash -cne $members['source/scripts/policy_owner/initialize_test_namespace.ps1'].sha256 -or $helperBytes.Length -ne [long]$members['source/scripts/policy_owner/initialize_test_namespace.ps1'].byte_length) {throw 'Reviewed namespace helper changed.'}
if ($ConsumerSha256 -cnotmatch '\A[0-9a-f]{64}\z' -or $PinLibrarySha256 -cnotmatch '\A[0-9a-f]{64}\z') {
    throw 'Exact consumer and pin-library SHA-256 values are required.'
}
$libraryBytes=[IO.File]::ReadAllBytes($PinLibraryPath)
$hasher=[Security.Cryptography.SHA256]::Create()
try {$libraryHash=[BitConverter]::ToString($hasher.ComputeHash($libraryBytes)).Replace('-','').ToLowerInvariant()}
finally {$hasher.Dispose()}
if ($libraryHash -cne $PinLibrarySha256) {throw 'Pin library bytes changed.'}
if ($libraryBytes.Length -ne [long]$members['source/scripts/policy_owner/pinned_setup_native.ps1'].byte_length) {throw 'Pin library length changed.'}
. ([scriptblock]::Create([Text.Encoding]::UTF8.GetString($libraryBytes)))
$api=New-SetupPinApi
$pins=[Collections.Generic.List[IDisposable]]::new()
try {
    $parents=[Collections.Generic.List[string]]::new()
    $parent=[IO.DirectoryInfo]::new([IO.Path]::GetDirectoryName($ConsumerPath))
    while ($null -ne $parent) {$parents.Add($parent.FullName);$parent=$parent.Parent}
    for ($index=$parents.Count-1;$index -ge 0;$index--) {
        $pins.Add((Open-SetupPin -Api $api -Path $parents[$index] -Directory $true))
    }
    $leaf=Open-SetupPin -Api $api -Path $ConsumerPath -Directory $false
    $pins.Add($leaf)
    $stream=[IO.FileStream]::new($leaf,[IO.FileAccess]::Read)
    $pins.Add($stream)
    $memory=[IO.MemoryStream]::new()
    try {$stream.CopyTo($memory);$assemblyBytes=$memory.ToArray()}
    finally {$memory.Dispose()}
    $hasher=[Security.Cryptography.SHA256]::Create()
    try {$actual=[BitConverter]::ToString($hasher.ComputeHash($assemblyBytes)).Replace('-','').ToLowerInvariant()}
    finally {$hasher.Dispose()}
    if ($actual -cne $ConsumerSha256) {throw 'Registered conditional assembly bytes changed.'}
    if ($assemblyBytes.Length -ne [long]$members['bin/ConditionalRuntimeProtectionHost.exe'].byte_length) {throw 'Reviewed conditional assembly length changed.'}
    $consumerAssembly=[Reflection.Assembly]::Load($assemblyBytes)
    $entry=$consumerAssembly.GetType('TradingAgents.RuntimeProtection.RuntimeProtectionHost',$true).GetMethod('Main')
    if ($entry.ReturnType -ne [int] -or -not $entry.IsStatic -or $entry.GetParameters().Length -ne 1 -or $entry.GetParameters()[0].ParameterType -ne [string[]]) {throw 'Native initializer entry point changed.'}
    $sourceHash=$null
    foreach($attribute in $consumerAssembly.GetCustomAttributesData()) {
        if ($attribute.AttributeType.FullName -ceq 'System.Reflection.AssemblyMetadataAttribute' -and $attribute.ConstructorArguments[0].Value -ceq 'RuntimeProtectionSourceSha256') {$sourceHash=[string]$attribute.ConstructorArguments[1].Value}
    }
    if ($sourceHash -cne $members['source/scripts/runtime_protection/RuntimeProtectionHost.cs'].sha256) {throw 'Native assembly/source ancestry differs from setup.'}
    $sourceIdentity='runtime-protection-source:v1:sha256:'+$sourceHash
    if ($PSCmdlet.ParameterSetName -eq 'ValidateOnly') {
        if (-not $ValidateOnly) {throw 'Explicit ValidateOnly is required; no initializer is invoked.'}
        Microsoft.PowerShell.Core\Import-Module -Name ([IO.Path]::Combine($PSHOME,'Modules','Microsoft.PowerShell.Utility','Microsoft.PowerShell.Utility.psd1')) -Scope Global -ErrorAction Stop
        [Console]::WriteLine((@{contract_version='registered-test-namespace-preflight-v1';instance=$Instance;deployment_sha256=$DeploymentSha256;consumer_path=$ConsumerPath;consumer_sha256=$actual;library_sha256=$libraryHash;helper_sha256=$helperHash;protection_source_identity=$sourceIdentity;staged_fixture=[bool]$StagedFixture;native_entrypoint_loaded=$true;namespace_initialized=$false;operational=$false;execution_authorized=$false} | ConvertTo-Json -Compress))
        return
    }
    $request=Read-ExactBinding $RequestPath $RequestSha256
    if ($request.contract_version -cne 'runtime-protection-namespace-request-v1' -or $request.protection_source_identity -cne $sourceIdentity) {throw 'Namespace request/source binding mismatch.'}
    if ($StagedFixture) {
        $fixtureRoot=[IO.Path]::Combine($bundle,'SYNTHETIC-namespace-fixtures')+'\'
        if ($RequestPath -cne [IO.Path]::GetFullPath($RequestPath) -or $request.namespace_root -cne [IO.Path]::GetFullPath($request.namespace_root) -or -not $RequestPath.StartsWith($fixtureRoot,[StringComparison]::Ordinal) -or -not $request.namespace_root.StartsWith($fixtureRoot,[StringComparison]::Ordinal)) {throw 'Staged rehearsal is confined to its disposable namespace fixture root.'}
    }
    # Resolve from this returned assembly, never a global PowerShell type lookup:
    # both native binaries define RuntimeProtectionHost.
    $arguments=[object[]]::new(1)
    $arguments[0]=[string[]]@('initialize',$RequestPath,$RequestSha256)
    $code=$entry.Invoke($null,$arguments)
    if ($code -ne 0) {throw ('Fresh TEST namespace initialization failed: '+$code+'; no retry performed.')}
}
finally {for ($index=$pins.Count-1;$index -ge 0;$index--) {$pins[$index].Dispose()}}

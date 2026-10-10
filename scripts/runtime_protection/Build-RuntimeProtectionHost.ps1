#requires -Version 5.1
#requires -PSEdition Desktop
<#! Build only the tracked protection host against the Windows .NET Framework.
    OutputDirectory must be new. No qualification or launch is performed. #>
[CmdletBinding()]
param([Parameter(Mandatory=$true)][string]$OutputDirectory)
$ErrorActionPreference='Stop'
. (Join-Path $PSScriptRoot 'ProtectionBootstrap.ps1')
function Get-Sha([string]$Path){
    $stream=[IO.File]::Open($Path,[IO.FileMode]::Open,[IO.FileAccess]::Read,[IO.FileShare]::Read)
    $hash=[Security.Cryptography.SHA256]::Create()
    try{return [BitConverter]::ToString($hash.ComputeHash($stream)).Replace('-','').ToLowerInvariant()}
    finally{$hash.Dispose();$stream.Dispose()}
}
$null=[Reflection.Assembly]::Load('System.Web.Extensions, Version=4.0.0.0, Culture=neutral, PublicKeyToken=31bf3856ad364e35')
$serializer=[Web.Script.Serialization.JavaScriptSerializer]::new()
$source=Join-Path $PSScriptRoot 'RuntimeProtectionHost.cs'
$runner=Join-Path $PSScriptRoot 'Invoke-RuntimeProtectionHost.ps1'
$output=[IO.Path]::GetFullPath($OutputDirectory)
if($output -notmatch '^[A-Z]:\\' -or $output -match '[\x00-\x1f]' -or $output.Substring(2).Contains(':')){throw 'Canonical local output directory required'}
Pin-ProtectionAncestors $output
if([IO.Directory]::Exists($output) -or [IO.File]::Exists($output)){throw 'Build output must be a new directory'}
$null=New-Item -ItemType Directory -Path $output -ErrorAction Stop
$compiler=Join-Path ([Environment]::GetFolderPath([Environment+SpecialFolder]::Windows)) 'Microsoft.NET\Framework64\v4.0.30319\csc.exe'
if(-not [IO.File]::Exists($compiler)){throw '64-bit Windows .NET Framework compiler required'}
$sourceHold=[IO.File]::Open($source,[IO.FileMode]::Open,[IO.FileAccess]::Read,[IO.FileShare]::Read)
try{
$null=Read-Pinned $source ''
$sourceHash=Get-Sha $source
$stamp=Join-Path $output 'BuildIdentity.cs'
$text='[assembly: System.Reflection.AssemblyMetadataAttribute("RuntimeProtectionSourceSha256", "'+$sourceHash+'")]'
$repository=[IO.Path]::GetFullPath((Join-Path $PSScriptRoot '..\..'))
$applicationBindings=@{
    RuntimeProtectionCliSha256=(Join-Path $repository 'cli\main.py')
    RuntimeProtectionContractSha256=(Join-Path $repository 'tradingagents\runtime_protection.py')
    RuntimeProtectionHandoffSha256=(Join-Path $repository 'tradingagents\runtime_protection_handoff.py')
}
$bindingHashes=[Collections.Generic.Dictionary[string,string]]::new([StringComparer]::Ordinal)
foreach($name in $applicationBindings.Keys){
    $null=Read-Pinned $applicationBindings[$name] ''
    $digest=Get-Sha $applicationBindings[$name]
    $bindingHashes.Add($name,[string]$digest)
    $text+='[assembly: System.Reflection.AssemblyMetadataAttribute("'+$name+'", "'+$digest+'")]'
}
[IO.File]::WriteAllText($stamp,$text,[Text.UTF8Encoding]::new($false))
$executable=Join-Path $output 'RuntimeProtectionHost.exe'
& $compiler '/nologo' '/target:exe' '/platform:x64' '/main:TradingAgents.RuntimeProtection.RuntimeProtectionHost' '/r:System.Web.Extensions.dll' ('/out:'+$executable) $source $stamp
if($LASTEXITCODE -ne 0){throw 'Closed protection host compilation failed'}
$manifest=[Collections.Generic.Dictionary[string,object]]::new([StringComparer]::Ordinal)
$manifest.Add('contract_version','runtime-protection-build-v1')
$manifest.Add('source_sha256',[string]$sourceHash)
$manifest.Add('builder_sha256',[string](Get-Sha $PSCommandPath))
$manifest.Add('runner_sha256',[string](Get-Sha $runner))
$manifest.Add('bootstrap_sha256',[string](Get-Sha (Join-Path $PSScriptRoot 'ProtectionBootstrap.ps1')))
$manifest.Add('executable_sha256',[string](Get-Sha $executable))
$manifest.Add('compiler_sha256',[string](Get-Sha $compiler))
$manifest.Add('protection_source_identity',('runtime-protection-source:v1:sha256:'+[string]$sourceHash))
$manifest.Add('application_bindings',$bindingHashes)
$manifest.Add('qualification_performed',[bool]$false)
$manifest.Add('execution_authorized',[bool]$false)
$manifestPath=Join-Path $output 'build.json'
[IO.File]::WriteAllText($manifestPath,$serializer.Serialize($manifest),[Text.UTF8Encoding]::new($false))
$reference=[Collections.Generic.Dictionary[string,string]]::new([StringComparer]::Ordinal)
$reference.Add('build_manifest',$manifestPath)
$reference.Add('sha256',[string](Get-Sha $manifestPath))
$serializer.Serialize($reference)
}finally{$sourceHold.Dispose();foreach($handle in $held){$handle.Dispose()}}

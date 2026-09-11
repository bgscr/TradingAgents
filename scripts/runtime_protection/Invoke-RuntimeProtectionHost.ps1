#requires -Version 5.1
#requires -PSEdition Desktop
<#! Load the hash-pinned host bytes in the existing trusted Windows platform.
    Never load a retained runtime DLL or execute preparation/history templates. #>
[CmdletBinding()]
param(
    [Parameter(Mandatory=$true)][string]$BuildManifestPath,
    [Parameter(Mandatory=$true)][ValidatePattern('^[0-9a-f]{64}$')][string]$BuildManifestSha256,
    [Parameter(Mandatory=$true)][ValidateSet('initialize','prepare','inspect','run')][string]$Operation,
    [Parameter(Mandatory=$true)][string]$InputPath,
    [Parameter(Mandatory=$true)][ValidatePattern('^[0-9a-f]{64}$')][string]$InputSha256,
    [string]$PacketPath,
    [ValidatePattern('^[0-9a-f]{64}$')][string]$PacketSha256
)
$ErrorActionPreference='Stop'
# This primitive is part of the trusted invoker. The external build helper is
# only read and hashed below; it is never executed by the launch entrypoint.
$held=[Collections.Generic.List[IDisposable]]::new()
Add-Type -TypeDefinition @'
using System;
using System.ComponentModel;
using System.Runtime.InteropServices;
using Microsoft.Win32.SafeHandles;
public static class RuntimeProtectionBootstrapPins {
    [StructLayout(LayoutKind.Sequential)] struct Info {
        public uint Attributes,CreationLow,CreationHigh,AccessLow,AccessHigh,
            WriteLow,WriteHigh,Volume,SizeHigh,SizeLow,Links,IndexHigh,IndexLow;
    }
    [DllImport("kernel32.dll",CharSet=CharSet.Unicode,SetLastError=true)]
    static extern SafeFileHandle CreateFileW(string path,uint access,uint share,IntPtr security,uint creation,uint flags,IntPtr template);
    [DllImport("kernel32.dll",SetLastError=true)] static extern bool GetFileInformationByHandle(SafeFileHandle handle,out Info value);
    public static SafeFileHandle Open(string path,bool directory) {
        var handle=CreateFileW("\\\\?\\"+path,directory?0x80u:0x80000000u,directory?3u:1u,IntPtr.Zero,3,0x02200000,IntPtr.Zero);
        try {
            if(handle.IsInvalid)throw new Win32Exception(Marshal.GetLastWin32Error());
            Info info;if(!GetFileInformationByHandle(handle,out info))throw new Win32Exception(Marshal.GetLastWin32Error());
            if((info.Attributes&0x400)!=0||((info.Attributes&0x10)!=0)!=directory||(!directory&&info.Links!=1))
                throw new InvalidOperationException("Build input reparse/hard-link/type mismatch");
            return handle;
        }catch {handle.Dispose();throw;}
    }
}
'@
function Pin-ProtectionAncestors([string]$Path){
    if($Path -notmatch '^[A-Z]:\\' -or $Path -match '[\x00-\x1f/]' -or $Path.Substring(2).Contains(':') -or $Path -cne [IO.Path]::GetFullPath($Path)){
        throw 'Canonical local build input required'
    }
    $parents=[Collections.Generic.List[string]]::new()
    $parent=[IO.Directory]::GetParent($Path)
    while($null -ne $parent){$parents.Add($parent.FullName);$parent=$parent.Parent}
    $parents.Reverse()
    foreach($directory in $parents){$held.Add([RuntimeProtectionBootstrapPins]::Open($directory,$true))}
}
function Read-Pinned([string]$Path,[string]$Expected){
    Pin-ProtectionAncestors $Path
    $leaf=[RuntimeProtectionBootstrapPins]::Open($Path,$false)
    try{$stream=[IO.FileStream]::new($leaf,[IO.FileAccess]::Read)}catch{$leaf.Dispose();throw}
    $held.Add($stream)
    if($stream.Length -gt 67108864){throw 'Build input exceeds size bound'}
    $memory=[IO.MemoryStream]::new()
    try{$stream.CopyTo($memory);$bytes=$memory.ToArray()}finally{$memory.Dispose()}
    $hash=[Security.Cryptography.SHA256]::Create()
    try{$actual=[BitConverter]::ToString($hash.ComputeHash($bytes)).Replace('-','').ToLowerInvariant()}finally{$hash.Dispose()}
    if($actual -cne $Expected){throw 'Pinned build input bytes differ'}
    return ,$bytes
}
try{
    $null=[Reflection.Assembly]::Load('System.Web.Extensions, Version=4.0.0.0, Culture=neutral, PublicKeyToken=31bf3856ad364e35')
    $serializer=[Web.Script.Serialization.JavaScriptSerializer]::new()
    $manifestBytes=Read-Pinned $BuildManifestPath $BuildManifestSha256
    $manifest=$serializer.DeserializeObject([Text.Encoding]::UTF8.GetString($manifestBytes))
    if($manifest.contract_version -cne 'runtime-protection-build-v1' -or $manifest.qualification_performed -or $manifest.execution_authorized){throw 'Build manifest contract/authority differs'}
    $null=Read-Pinned (Join-Path $PSScriptRoot 'RuntimeProtectionHost.cs') $manifest.source_sha256
    $null=Read-Pinned (Join-Path $PSScriptRoot 'ProtectionBootstrap.ps1') $manifest.bootstrap_sha256
    $null=Read-Pinned (Join-Path $PSScriptRoot 'Build-RuntimeProtectionHost.ps1') $manifest.builder_sha256
    $null=Read-Pinned $PSCommandPath $manifest.runner_sha256
    $repository=[IO.Path]::GetFullPath((Join-Path $PSScriptRoot '..\..'))
    $null=Read-Pinned (Join-Path $repository 'cli\main.py') $manifest.application_bindings.RuntimeProtectionCliSha256
    $null=Read-Pinned (Join-Path $repository 'tradingagents\runtime_protection.py') $manifest.application_bindings.RuntimeProtectionContractSha256
    $null=Read-Pinned (Join-Path $repository 'tradingagents\runtime_protection_handoff.py') $manifest.application_bindings.RuntimeProtectionHandoffSha256
    $bytes=Read-Pinned (Join-Path ([IO.Path]::GetDirectoryName($BuildManifestPath)) 'RuntimeProtectionHost.exe') $manifest.executable_sha256
    # Platform/GAC references, then exact in-memory assembly bytes. Adjacent
    # executable configuration and DLL probing are not build dependencies.
    $null=[Reflection.Assembly]::Load('System.Web.Extensions, Version=4.0.0.0, Culture=neutral, PublicKeyToken=31bf3856ad364e35')
    $assembly=[Reflection.Assembly]::Load($bytes)
    [string[]]$arguments=@($Operation,$InputPath,$InputSha256)
    if($Operation -eq 'run'){
        if(-not $PacketPath -or -not $PacketSha256){throw 'Run requires the exact packet bytes'}
        $arguments+=@($PacketPath,$PacketSha256)
    }elseif($PacketPath -or $PacketSha256){throw 'Unexpected packet for read/preparation operation'}
    $exitCode=[int]$assembly.EntryPoint.Invoke($null,@(,$arguments))
}finally{
    foreach($handle in $held){$handle.Dispose()}
}
exit $exitCode

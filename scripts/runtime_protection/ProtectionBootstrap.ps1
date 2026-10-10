# Read-only bootstrap pins. No protection, WRITE_DAC, or restoration operation.
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
    if($Expected -and $actual -cne $Expected){throw 'Pinned build input bytes differ'}
    return ,$bytes
}

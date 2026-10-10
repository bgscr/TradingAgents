# Embedded in the byte-verified human action. Defines functions only.
# P/Invoke stubs are emitted in memory; no compiler, DLL output or dependency.
function New-SetupPinApi {
    $assemblyName = [System.Reflection.AssemblyName]::new('TASetupPins' + [Guid]::NewGuid().ToString('N'))
    $assembly = [System.Reflection.Emit.AssemblyBuilder]::DefineDynamicAssembly($assemblyName, [System.Reflection.Emit.AssemblyBuilderAccess]::Run)
    $module = $assembly.DefineDynamicModule($assemblyName.Name)
    $type = $module.DefineType('NativePins', [System.Reflection.TypeAttributes]::Public)
    $attributes = [System.Reflection.MethodAttributes]::Public -bor [System.Reflection.MethodAttributes]::Static -bor [System.Reflection.MethodAttributes]::PinvokeImpl
    $methods = @(
        @{ Name='CreateFileW'; Return=[IntPtr]; Parameters=[Type[]]@([string],[uint32],[uint32],[IntPtr],[uint32],[uint32],[IntPtr]); Charset=[System.Runtime.InteropServices.CharSet]::Unicode },
        @{ Name='GetFileInformationByHandle'; Return=[bool]; Parameters=[Type[]]@([IntPtr],[IntPtr]); Charset=[System.Runtime.InteropServices.CharSet]::None }
    )
    foreach ($method in $methods) {
        $builder = $type.DefinePInvokeMethod($method.Name, 'kernel32.dll', $attributes, [System.Reflection.CallingConventions]::Standard, $method.Return, $method.Parameters, [System.Runtime.InteropServices.CallingConvention]::Winapi, $method.Charset)
        $builder.SetImplementationFlags([System.Reflection.MethodImplAttributes]::PreserveSig)
    }
    return $type.CreateType()
}

function Open-SetupPin {
    param([Type] $Api, [string] $Path, [bool] $Directory)
    [uint32] $access = if ($Directory) { 128 } else { 2147483648 }
    [uint32] $share = if ($Directory) { 3 } else { 1 }
    $pointer = $Api.GetMethod('CreateFileW').Invoke($null, [object[]]@($Path,$access,$share,[IntPtr]::Zero,[uint32]3,[uint32]0x02200000,[IntPtr]::Zero))
    $handle = [Microsoft.Win32.SafeHandles.SafeFileHandle]::new($pointer, $true)
    if ($handle.IsInvalid) { $handle.Dispose(); throw ('Cannot pin setup object: ' + $Path) }
    $information = [System.Runtime.InteropServices.Marshal]::AllocHGlobal(52)
    try {
        $ok = $Api.GetMethod('GetFileInformationByHandle').Invoke($null, [object[]]@($handle.DangerousGetHandle(),$information))
        if (-not $ok) { throw ('Cannot inspect setup object: ' + $Path) }
        $flags = [System.Runtime.InteropServices.Marshal]::ReadInt32($information, 0)
        $links = [System.Runtime.InteropServices.Marshal]::ReadInt32($information, 40)
        if (($flags -band 0x400) -ne 0 -or (($flags -band 0x10) -ne 0) -ne $Directory -or (-not $Directory -and $links -ne 1)) {
            throw ('Reparse, object type or hard-link mismatch in setup path: ' + $Path)
        }
        return $handle
    }
    catch { $handle.Dispose(); throw }
    finally { [System.Runtime.InteropServices.Marshal]::FreeHGlobal($information) }
}

function Invoke-PinnedSetupNative {
    param([string] $Path, [string] $Sha256, [string[]] $Arguments, [switch] $InProcessSetup)
    $fullPath = [System.IO.Path]::GetFullPath($Path)
    if ($fullPath -notmatch '^[A-Za-z]:\\' -or $fullPath -match '[<>|*?]') { throw 'An absolute local reviewed executable path is required.' }
    $api = New-SetupPinApi
    $pins = [System.Collections.Generic.List[System.IDisposable]]::new()
    $parents = [System.Collections.Generic.List[string]]::new()
    $parent = [System.IO.DirectoryInfo]::new([System.IO.Path]::GetDirectoryName($fullPath))
    while ($null -ne $parent) { $parents.Add($parent.FullName); $parent = $parent.Parent }
    try {
        for ($index = $parents.Count - 1; $index -ge 0; $index--) {
            $pins.Add((Open-SetupPin -Api $api -Path $parents[$index] -Directory $true))
        }
        $leaf = Open-SetupPin -Api $api -Path $fullPath -Directory $false
        $pins.Add($leaf)
        $stream = [System.IO.FileStream]::new($leaf, [System.IO.FileAccess]::Read)
        $pins.Add($stream)
        $hasher = [System.Security.Cryptography.SHA256]::Create()
        try { $actual = [BitConverter]::ToString($hasher.ComputeHash($stream)).Replace('-','').ToLowerInvariant() }
        finally { $hasher.Dispose() }
        if ($actual -ne $Sha256) { throw 'Exact reviewed native executable bytes changed.' }
        if ($InProcessSetup) {
            # The first setup assembly resides outside the protected owner root.
            # Execute its verified bytes in this existing trusted Framework host,
            # so writable exe.config/CLR startup sidecars cannot run before Main.
            if ($PSVersionTable.PSEdition -ne 'Desktop' -or [Threading.Thread]::CurrentThread.ApartmentState -ne 'STA' -or [IntPtr]::Size -ne 8) {
                throw 'Use elevated 64-bit Windows PowerShell 5.1 (STA) for the reviewed setup bootstrap.'
            }
            $stream.Position = 0
            $memory = [IO.MemoryStream]::new()
            try { $stream.CopyTo($memory); $assemblyBytes = $memory.ToArray() }
            finally { $memory.Dispose() }
            $setupAssembly = [Reflection.Assembly]::Load($assemblyBytes)
            $entryArguments = [object[]]::new(1)
            $entryArguments[0] = [string[]]$Arguments
            $code = $setupAssembly.EntryPoint.Invoke($null, $entryArguments)
            if ($code -ne 0) { throw ('Verified setup entry point failed with exit ' + $code + '; no retry performed.') }
            return
        }
        # The same leaf and every ancestor remain held until this process exits.
        & $fullPath @Arguments
        if ($LASTEXITCODE -ne 0) { throw ('Pinned native action failed with exit ' + $LASTEXITCODE + '; no retry performed.') }
    }
    finally {
        for ($index = $pins.Count - 1; $index -ge 0; $index--) { $pins[$index].Dispose() }
    }
}

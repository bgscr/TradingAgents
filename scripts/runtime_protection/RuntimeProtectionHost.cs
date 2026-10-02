// Host-side runtime protection. Only Windows/.NET platform code is loaded before
// the entire private payload is verified and protected through child termination.
using System;
using System.Collections.Generic;
using System.Collections;
using System.ComponentModel;
using System.Diagnostics;
using System.Globalization;
using System.IO;
using System.IO.Pipes;
using System.Reflection;
using System.Runtime.InteropServices;
using System.Security.AccessControl;
using System.Security.Cryptography;
using System.Security.Principal;
using System.Text;
using System.Text.RegularExpressions;
using System.Threading.Tasks;
using System.Web.Script.Serialization;
using Microsoft.Win32.SafeHandles;

namespace TradingAgents.RuntimeProtection
{
    internal static class Native
    {
        static Native()
        {
            AppContext.SetSwitch("Switch.System.IO.UseLegacyPathHandling",false);
            AppContext.SetSwitch("Switch.System.IO.BlockLongPaths",false);
        }
        // Force the initializer before an entrypoint's first managed path API;
        // Framework compatibility switches are cached on their first use.
        internal static void InitializePathRuntime() { }
        internal const uint ReadData=1, ReadAttributes=0x80, ReadControl=0x20000, WriteDac=0x40000;
        internal const uint Backup=0x02000000, Reparse=0x00200000, Dacl=4, ProtectedDacl=0x80000000;
        internal const string SealedDacl="D:P(D;;0x000D0156;;;WD)(D;;WD;;;OW)(A;;0x001200A9;;;WD)";
        [StructLayout(LayoutKind.Sequential)] internal struct SecurityAttributes
        { internal int Length; internal IntPtr Descriptor; internal int Inherit; }
        [StructLayout(LayoutKind.Sequential)] struct UnicodeString
        { internal ushort Length,MaximumLength;internal IntPtr Buffer; }
        [StructLayout(LayoutKind.Sequential)] struct ObjectAttributes
        { internal int Length;internal IntPtr RootDirectory,ObjectName;internal uint Attributes;internal IntPtr SecurityDescriptor,SecurityQualityOfService; }
        [StructLayout(LayoutKind.Sequential)] struct IoStatusBlock
        { internal IntPtr Status,Information; }
        [StructLayout(LayoutKind.Sequential)] internal struct FileInformation
        {
            internal uint Attributes, CreationLow, CreationHigh, AccessLow, AccessHigh,
                WriteLow, WriteHigh, Volume, SizeHigh, SizeLow, Links, IndexHigh, IndexLow;
        }
        [DllImport("kernel32.dll",CharSet=CharSet.Unicode,SetLastError=true)]
        internal static extern SafeFileHandle CreateFileW(string path,uint access,uint share,
            IntPtr security,uint creation,uint flags,IntPtr template);
        [StructLayout(LayoutKind.Sequential,CharSet=CharSet.Unicode)]
        struct FindData
        {
            internal uint Attributes,CreationLow,CreationHigh,AccessLow,AccessHigh,WriteLow,WriteHigh,SizeHigh,SizeLow,Reserved0,Reserved1;
            [MarshalAs(UnmanagedType.ByValTStr,SizeConst=260)] internal string Name;
            [MarshalAs(UnmanagedType.ByValTStr,SizeConst=14)] internal string AlternateName;
        }
        [DllImport("kernel32.dll",CharSet=CharSet.Unicode,SetLastError=true)] static extern uint GetFileAttributesW(string path);
        [DllImport("kernel32.dll",CharSet=CharSet.Unicode,SetLastError=true)] static extern IntPtr FindFirstFileW(string pattern,out FindData data);
        [DllImport("kernel32.dll",CharSet=CharSet.Unicode,SetLastError=true)] static extern bool FindNextFileW(IntPtr find,out FindData data);
        [DllImport("kernel32.dll",SetLastError=true)] static extern bool FindClose(IntPtr find);
        [DllImport("ntdll.dll")]
        static extern int NtCreateFile(out SafeFileHandle handle,uint access,ref ObjectAttributes attributes,
            out IoStatusBlock status,IntPtr allocation,uint fileAttributes,uint share,uint disposition,
            uint options,IntPtr extendedAttributes,uint extendedAttributesLength);
        [DllImport("ntdll.dll")] static extern uint RtlNtStatusToDosError(int status);
        [DllImport("advapi32.dll",CharSet=CharSet.Unicode,SetLastError=true)]
        static extern bool ConvertStringSecurityDescriptorToSecurityDescriptorW(
            string text,uint revision,out IntPtr descriptor,out uint size);
        [DllImport("advapi32.dll",SetLastError=true)]
        static extern uint GetSecurityInfo(SafeFileHandle handle,uint type,uint information,
            out IntPtr owner,out IntPtr group,out IntPtr dacl,out IntPtr sacl,out IntPtr descriptor);
        [DllImport("advapi32.dll",SetLastError=true)]
        static extern uint GetSecurityDescriptorLength(IntPtr descriptor);
        [DllImport("advapi32.dll",SetLastError=true)]
        static extern bool SetKernelObjectSecurity(SafeFileHandle handle,uint information,byte[] descriptor);
        [DllImport("kernel32.dll")] static extern IntPtr LocalFree(IntPtr pointer);
        [DllImport("kernel32.dll",SetLastError=true)]
        static extern bool GetFileInformationByHandle(SafeFileHandle handle,out FileInformation information);
        [DllImport("kernel32.dll",SetLastError=true)]
        static extern bool ReadFile(SafeFileHandle handle,byte[] buffer,uint count,out uint read,IntPtr overlapped);
        [DllImport("kernel32.dll",SetLastError=true)]
        static extern bool SetFilePointerEx(SafeFileHandle handle,long distance,out long position,uint method);
        [DllImport("kernel32.dll",CharSet=CharSet.Unicode,SetLastError=true)]
        internal static extern uint GetDllDirectoryW(uint length,StringBuilder buffer);
        [DllImport("kernel32.dll",CharSet=CharSet.Unicode,SetLastError=true)]
        internal static extern bool SetDllDirectoryW(string path);
        [DllImport("kernel32.dll",SetLastError=true)]
        internal static extern bool GetNamedPipeClientProcessId(SafePipeHandle pipe,out uint processId);
        [StructLayout(LayoutKind.Sequential)] struct TokenSidAndAttributes { internal IntPtr Sid;internal uint Attributes; }
        [StructLayout(LayoutKind.Sequential)] struct TokenGroupFirst { internal uint Count;internal TokenSidAndAttributes First; }
        [DllImport("advapi32.dll",SetLastError=true)]
        static extern bool GetTokenInformation(SafeAccessTokenHandle token,int kind,IntPtr value,uint length,out uint needed);
        static List<KeyValuePair<string,uint>> TokenGroups(SafeAccessTokenHandle token,int informationClass=2)
        {
            // Query TOKEN_GROUPS directly. WindowsPrincipal may duplicate a
            // primary token; readers must never receive TOKEN_DUPLICATE on the
            // owner service merely to authenticate its enabled service SID.
            uint needed;GetTokenInformation(token,informationClass,IntPtr.Zero,0,out needed);
            if(needed==0||needed>4*1024*1024)throw new InvalidOperationException("Token group information unavailable");
            IntPtr buffer=Marshal.AllocHGlobal((int)needed);
            try
            {
                if(!GetTokenInformation(token,informationClass,buffer,needed,out needed))throw new Win32Exception(Marshal.GetLastWin32Error());
                uint count=unchecked((uint)Marshal.ReadInt32(buffer));int offset=Marshal.OffsetOf(typeof(TokenGroupFirst),"First").ToInt32();
                int stride=Marshal.SizeOf(typeof(TokenSidAndAttributes));
                if(count>4096||offset+(long)count*stride>needed)throw new InvalidOperationException("Invalid token group extent");
                var result=new List<KeyValuePair<string,uint>>();
                for(int index=0;index<count;index++)
                {
                    var group=(TokenSidAndAttributes)Marshal.PtrToStructure(IntPtr.Add(buffer,offset+index*stride),typeof(TokenSidAndAttributes));
                    result.Add(new KeyValuePair<string,uint>(new SecurityIdentifier(group.Sid).Value,group.Attributes));
                }
                return result;
            }
            finally { Marshal.FreeHGlobal(buffer); }
        }
        internal static bool TokenHasEnabledSid(SafeAccessTokenHandle token,string sid)
        {
            foreach(var group in TokenGroups(token))if(group.Key==sid&&(group.Value&4)!=0&&(group.Value&16)==0)return true;
            return false;
        }
        internal static string[] TokenRestrictingSids(SafeAccessTokenHandle token)
        { var values=new List<string>();foreach(var entry in TokenGroups(token,11))values.Add(entry.Key);return values.ToArray(); }

        internal static byte[] FromSddl(string sddl)
        {
            IntPtr pointer; uint size;
            if(!ConvertStringSecurityDescriptorToSecurityDescriptorW(sddl,1,out pointer,out size))
                throw new Win32Exception(Marshal.GetLastWin32Error());
            try { var bytes=new byte[size]; Marshal.Copy(pointer,bytes,0,bytes.Length); return bytes; }
            finally { LocalFree(pointer); }
        }
        internal static void CreateNewDirectory(string path,bool immutable)
        { using(var handle=CreateNewDirectoryHandle(path,immutable)) { } }
        internal static SafeFileHandle CreateNewDirectoryHandle(string path,bool immutable,string initialSddl=null)
        {
            LocalPath(path,false);
            string sid=WindowsIdentity.GetCurrent().User.Value;
            // The baseline is installed AT CREATION, before it is captured. Its
            // protected DACL avoids inheritance canonicalization during restoration.
            string sddl=initialSddl??(immutable
                ? "D:P(D;;0x000D0040;;;WD)(D;;WD;;;OW)(A;;0x001200AF;;;"+sid+")(A;;0x001200AF;;;SY)"
                : "D:P(A;;FA;;;"+sid+")(A;;FA;;;SY)");
            byte[] bytes=FromSddl(sddl);
            var pin=GCHandle.Alloc(bytes,GCHandleType.Pinned);
            string ntPath="\\??\\"+path;IntPtr text=Marshal.StringToHGlobalUni(ntPath);
            IntPtr name=Marshal.AllocHGlobal(Marshal.SizeOf(typeof(UnicodeString)));
            try
            {
                Marshal.StructureToPtr(new UnicodeString { Length=checked((ushort)(ntPath.Length*2)),MaximumLength=checked((ushort)(ntPath.Length*2+2)),Buffer=text },name,false);
                var attributes=new ObjectAttributes { Length=Marshal.SizeOf(typeof(ObjectAttributes)),ObjectName=name,
                    Attributes=0x40,SecurityDescriptor=pin.AddrOfPinnedObject() };
                SafeFileHandle handle;IoStatusBlock status;
                // FILE_CREATE is exclusive. The returned handle is the actual
                // created directory, never a later pathname-based replacement.
                int result=NtCreateFile(out handle,ReadData|ReadAttributes|ReadControl|0x100000|(immutable?0:WriteDac),
                    ref attributes,out status,IntPtr.Zero,0x80,3,2,1|0x20|Reparse,IntPtr.Zero,0);
                if(result<0) { if(handle!=null)handle.Dispose();throw new Win32Exception((int)RtlNtStatusToDosError(result),"Fresh directory creation rejected: "+path); }
                return handle;
            }
            finally { Marshal.FreeHGlobal(name);Marshal.FreeHGlobal(text);pin.Free(); }
        }
        internal static SafeFileHandle CreateNewOwnerFileHandle(string path,string sddl,bool writeThrough=false)
        {
            LocalPath(path,false);byte[] bytes=FromSddl(sddl);var pin=GCHandle.Alloc(bytes,GCHandleType.Pinned);
            string ntPath="\\??\\"+path;IntPtr text=Marshal.StringToHGlobalUni(ntPath);
            IntPtr name=Marshal.AllocHGlobal(Marshal.SizeOf(typeof(UnicodeString)));
            try
            {
                Marshal.StructureToPtr(new UnicodeString { Length=checked((ushort)(ntPath.Length*2)),MaximumLength=checked((ushort)(ntPath.Length*2+2)),Buffer=text },name,false);
                var attributes=new ObjectAttributes { Length=Marshal.SizeOf(typeof(ObjectAttributes)),ObjectName=name,Attributes=0x40,SecurityDescriptor=pin.AddrOfPinnedObject() };
                SafeFileHandle handle;IoStatusBlock status;
                int result=NtCreateFile(out handle,ReadData|2u|ReadAttributes|ReadControl|WriteDac|0x100000,ref attributes,out status,IntPtr.Zero,0x80,1,2,0x40|0x20|Reparse|(writeThrough?2u:0u),IntPtr.Zero,0);
                if(result<0) { if(handle!=null)handle.Dispose();throw new Win32Exception((int)RtlNtStatusToDosError(result),"Exclusive protected owner file creation failed"); }
                return handle;
            }
            finally { Marshal.FreeHGlobal(name);Marshal.FreeHGlobal(text);pin.Free(); }
        }
        internal static string LocalPath(string value,bool allowDriveRoot)
        {
            if(value==null||!Regex.IsMatch(value,"^[A-Z]:\\\\")||value.IndexOf('/')>=0||value.IndexOfAny(new char[]{'*','?','"','<','>','|'})>=0)
                throw new InvalidDataException("Canonical local Windows path required");
            if(allowDriveRoot&&value.Length==3)return value;
            if(value.Length<=3||value.EndsWith("\\")||value.Substring(2).IndexOf(':')>=0||Path.GetFullPath(value)!=value)
                throw new InvalidDataException("Noncanonical local path");
            foreach(char character in value)if(character<32)throw new InvalidDataException("Control character in path");
            foreach(string part in value.Substring(3).Split('\\'))
                if(part==""||part=="."||part==".."||part.EndsWith(".")||part.EndsWith(" ")||Regex.IsMatch(part,"^(CON|PRN|AUX|NUL|COM[1-9]|LPT[1-9])(?:\\..*)?$",RegexOptions.IgnoreCase))
                    throw new InvalidDataException("Reserved/ambiguous path component");
            return value;
        }
        static bool Exists(string path,bool directory)
        {
            LocalPath(path,false);uint attributes=GetFileAttributesW("\\\\?\\"+path);
            if(attributes==UInt32.MaxValue)
            {
                int error=Marshal.GetLastWin32Error();
                if(error==2||error==3)return false;
                throw new Win32Exception(error,"Cannot inspect protection object: "+path);
            }
            if((attributes&0x400)!=0)throw new InvalidOperationException("Reparse protection object rejected: "+path);
            return ((attributes&0x10)!=0)==directory;
        }
        internal static bool DirectoryExists(string path) { return Exists(path,true); }
        internal static bool FileExists(string path) { return Exists(path,false); }
        internal static string[] DirectoryEntries(string path)
        {
            LocalPath(path,false);FindData data;IntPtr find=FindFirstFileW("\\\\?\\"+path+"\\*",out data);
            if(find==new IntPtr(-1))
            {
                int error=Marshal.GetLastWin32Error();
                if(error==2)return new string[0];
                throw new Win32Exception(error,"Cannot enumerate protection directory: "+path);
            }
            var values=new List<string>();
            try
            {
                do { if(data.Name!="."&&data.Name!="..")values.Add(LocalPath(path+"\\"+data.Name,false)); }
                while(FindNextFileW(find,out data));
                int error=Marshal.GetLastWin32Error();
                if(error!=18)throw new Win32Exception(error,"Incomplete protection directory enumeration: "+path);
            }
            finally { FindClose(find); }
            return values.ToArray();
        }
        internal static SafeFileHandle Open(string path,uint access,uint share)
        {
            LocalPath(path,true);
            var handle=CreateFileW("\\\\?\\"+path,access,share,IntPtr.Zero,3,Backup|Reparse,IntPtr.Zero);
            if(handle.IsInvalid)
            {
                int error=Marshal.GetLastWin32Error(); handle.Dispose();
                throw new Win32Exception(error,"Cannot open protection object: "+path);
            }
            try { Information(handle); return handle; }
            catch { handle.Dispose(); throw; }
        }
        internal static FileInformation Information(SafeFileHandle handle)
        {
            FileInformation value;
            if(!GetFileInformationByHandle(handle,out value)) throw new Win32Exception(Marshal.GetLastWin32Error());
            if((value.Attributes&0x400)!=0) throw new InvalidOperationException("Reparse object rejected");
            if(value.Links!=1 && (value.Attributes&0x10)==0) throw new InvalidOperationException("Hard-linked payload rejected");
            return value;
        }
        internal static SafeFileHandle OpenWindowsPlatformImage(string path)
        {
            LocalPath(path,false);string windows=Environment.GetFolderPath(Environment.SpecialFolder.Windows);
            if(!path.StartsWith(Path.Combine(windows,"System32")+"\\",StringComparison.OrdinalIgnoreCase)
                &&!path.StartsWith(Path.Combine(windows,"WinSxS")+"\\",StringComparison.OrdinalIgnoreCase))
                throw new InvalidOperationException("Unqualified loaded native image origin");
            var handle=CreateFileW("\\\\?\\"+path,ReadData|ReadControl|ReadAttributes,1,IntPtr.Zero,3,Backup|Reparse,IntPtr.Zero);
            try
            {
                if(handle.IsInvalid)throw new Win32Exception(Marshal.GetLastWin32Error());
                FileInformation info;
                if(!GetFileInformationByHandle(handle,out info))throw new Win32Exception(Marshal.GetLastWin32Error());
                if((info.Attributes&(0x400|0x10))!=0)throw new InvalidOperationException("Platform reparse/directory image rejected");
                string owner=new RawSecurityDescriptor(Descriptor(handle),0).Owner.Value;
                if(owner!="S-1-5-18"&&owner!="S-1-5-80-956008885-3418522649-1831038044-1853292631-2271478464")
                    throw new InvalidOperationException("Platform image is not Windows-owned");
                // Windows servicing hard-links System32 to WinSxS. This is the
                // existing OS ownership boundary, not private payload ancestry.
                return handle;
            }
            catch { handle.Dispose();throw; }
        }
        internal static string ObjectId(SafeFileHandle handle)
        {
            var info=Information(handle);
            return info.Volume.ToString("x8")+":"+info.IndexHigh.ToString("x8")+info.IndexLow.ToString("x8");
        }
        internal static byte[] Descriptor(SafeFileHandle handle)
        {
            IntPtr owner,group,dacl,sacl,descriptor;
            uint result=GetSecurityInfo(handle,1,1|2|4,out owner,out group,out dacl,out sacl,out descriptor);
            if(result!=0) throw new Win32Exception((int)result);
            try
            {
                var bytes=new byte[GetSecurityDescriptorLength(descriptor)];
                Marshal.Copy(descriptor,bytes,0,bytes.Length); return bytes;
            }
            finally { LocalFree(descriptor); }
        }
        internal static void SetDacl(SafeFileHandle handle,byte[] descriptor)
        {
            if(!SetKernelObjectSecurity(handle,Dacl|ProtectedDacl,descriptor))
                throw new Win32Exception(Marshal.GetLastWin32Error());
        }
        internal static string Hash(byte[] bytes)
        { using(var hash=SHA256.Create()) return BitConverter.ToString(hash.ComputeHash(bytes)).Replace("-","").ToLowerInvariant(); }
        internal static string HashFile(SafeFileHandle handle)
        {
            long position;
            if(!SetFilePointerEx(handle,0,out position,0)) throw new Win32Exception(Marshal.GetLastWin32Error());
            using(var hash=SHA256.Create())
            {
                byte[] buffer=new byte[1024*1024]; uint read;
                do
                {
                    if(!ReadFile(handle,buffer,(uint)buffer.Length,out read,IntPtr.Zero))
                        throw new Win32Exception(Marshal.GetLastWin32Error());
                    if(read>0) hash.TransformBlock(buffer,0,(int)read,null,0);
                } while(read>0);
                hash.TransformFinalBlock(new byte[0],0,0);
                return BitConverter.ToString(hash.Hash).Replace("-","").ToLowerInvariant();
            }
        }
        internal static bool Equal(byte[] a,byte[] b)
        { if(a.Length!=b.Length)return false; for(int i=0;i<a.Length;i++)if(a[i]!=b[i])return false; return true; }
        internal static bool HasSealedDacl(SafeFileHandle handle)
        {
            var actual=new RawSecurityDescriptor(Descriptor(handle),0);
            var expected=new RawSecurityDescriptor(SealedDacl);
            if((actual.ControlFlags&ControlFlags.DiscretionaryAclProtected)==0||actual.DiscretionaryAcl==null)return false;
            byte[] a=new byte[actual.DiscretionaryAcl.BinaryLength],b=new byte[expected.DiscretionaryAcl.BinaryLength];
            actual.DiscretionaryAcl.GetBinaryForm(a,0);expected.DiscretionaryAcl.GetBinaryForm(b,0);
            return Equal(a,b);
        }
        internal static string Relative(string root,string path)
        { return path==root?"":path.Substring(root.Length+1).Replace('\\','/'); }
        internal static string Child(string root,string relative)
        {
            if(String.IsNullOrEmpty(relative)||relative.IndexOfAny(new char[]{'\\',':','*','?','"','<','>','|'})>=0)
                throw new InvalidOperationException("Invalid relative payload path");
            foreach(string part in relative.Split('/'))
                if(part==""||part=="."||part==".."||part.EndsWith(".")||part.EndsWith(" "))
                    throw new InvalidOperationException("Invalid relative payload component");
            string path=Path.GetFullPath(Path.Combine(root,relative));
            if(!path.StartsWith(root+"\\",StringComparison.OrdinalIgnoreCase))throw new InvalidOperationException("Payload escape");
            return LocalPath(path,false);
        }
        internal static List<SafeFileHandle> Anchor(string root)
        {
            LocalPath(root,false);
            var result=new List<SafeFileHandle>(); var parents=new List<string>();
            DirectoryInfo parent=new DirectoryInfo(root).Parent;
            while(parent!=null) { parents.Add(parent.FullName); parent=parent.Parent; }
            parents.Reverse();
            try
            {
                foreach(string path in parents)result.Add(Open(path,ReadAttributes,3));
                return result;
            }
            catch { foreach(var handle in result)handle.Dispose(); throw; }
        }
    }

    internal sealed class BaselineEntry
    {
        public string RelativePath { get; internal set; }
        public bool Directory { get; internal set; }
        public string ObjectIdentity { get; internal set; }
        public string DescriptorIdentity { get; internal set; }
        public int ControlFlags { get; internal set; }
        public string ContentSha256 { get; internal set; }
    }

    internal sealed class NativePayloadClosure
    {
        readonly HashSet<string> privateNames=new HashSet<string>(StringComparer.OrdinalIgnoreCase);
        readonly HashSet<string> checkedPlatform=new HashSet<string>(StringComparer.OrdinalIgnoreCase);
        [DllImport("kernel32.dll",CharSet=CharSet.Unicode,SetLastError=true)] static extern IntPtr LoadLibraryExW(string name,IntPtr file,uint flags);
        [DllImport("kernel32.dll",CharSet=CharSet.Unicode,SetLastError=true)] static extern uint GetModuleFileNameW(IntPtr module,StringBuilder path,int length);
        [DllImport("kernel32.dll")] static extern bool FreeLibrary(IntPtr module);
        internal NativePayloadClosure(params Dictionary<string,object>[] components)
        {
            foreach(var component in components)
                foreach(object name in (IEnumerable)component["native_images"])privateNames.Add(Path.GetFileName(Evidence.Str(name)));
        }
        static void Bounds(byte[] bytes,long offset,long length)
        { if(offset<0||length<0||offset>bytes.Length-length)throw new InvalidDataException("Malformed native dependency table"); }
        static ushort U16(byte[] bytes,int offset) { Bounds(bytes,offset,2);return BitConverter.ToUInt16(bytes,offset); }
        static uint U32(byte[] bytes,int offset) { Bounds(bytes,offset,4);return BitConverter.ToUInt32(bytes,offset); }
        static int Offset(byte[] bytes,uint rva,int sections,int count,uint headers)
        {
            if(rva<headers) { Bounds(bytes,rva,1);return checked((int)rva); }
            for(int i=0;i<count;i++)
            {
                int section=checked(sections+i*40);uint address=U32(bytes,section+12),rawSize=U32(bytes,section+16),raw=U32(bytes,section+20);
                if(rva>=address&&(ulong)rva-address<rawSize)
                { long offset=(long)raw+rva-address;Bounds(bytes,offset,1);return checked((int)offset); }
            }
            throw new InvalidDataException("Unmapped native dependency address");
        }
        void Dependency(string name)
        {
            if(!Regex.IsMatch(name,"^[A-Za-z0-9_.-]+\\.dll$",RegexOptions.IgnoreCase))throw new InvalidDataException("Invalid native dependency name");
            if(privateNames.Contains(name)||checkedPlatform.Contains(name))return;
            string system=Path.Combine(Environment.GetFolderPath(Environment.SpecialFolder.Windows),"System32",name);
            try
            {
                if(!File.Exists(system)&&(name.StartsWith("api-ms-win-",StringComparison.OrdinalIgnoreCase)||name.StartsWith("ext-ms-win-",StringComparison.OrdinalIgnoreCase)))
                {
                    // Resolve OS API-set aliases using only System32. Mapping
                    // without resolving imports runs no DLL initialization.
                    IntPtr module=LoadLibraryExW(name,IntPtr.Zero,0x800|1);
                    if(module==IntPtr.Zero)throw new Win32Exception(Marshal.GetLastWin32Error());
                    try
                    {
                        var path=new StringBuilder(32768);uint length=GetModuleFileNameW(module,path,path.Capacity);
                        if(length==0||length>=path.Capacity)throw new InvalidDataException("Native API-set origin unavailable");
                        system=path.ToString();
                    }
                    finally { FreeLibrary(module); }
                }
                using(var pins=new AncestorPins(system))
                using(var handle=Native.OpenWindowsPlatformImage(system)) { }
                checkedPlatform.Add(name);
            }
            catch(Exception error) { throw new InvalidOperationException("Unknown or unqualified native dependency: "+name,error); }
        }
        internal void Verify(byte[] bytes)
        {
            if(bytes.Length<2||bytes[0]!=77||bytes[1]!=90)return;
            int pe=checked((int)U32(bytes,60));
            if(U32(bytes,pe)!=0x4550||U16(bytes,pe+4)!=0x8664)throw new InvalidDataException("Unsupported native dependency image/architecture");
            int count=U16(bytes,pe+6),optional=checked(pe+24),size=U16(bytes,pe+20),sections=checked(optional+size);
            Bounds(bytes,sections,(long)count*40);
            if(U16(bytes,optional)!=0x20b||size<112)throw new InvalidDataException("Invalid native dependency optional header");
            uint headers=U32(bytes,optional+60),directories=U32(bytes,optional+108);
            foreach(int index in new int[]{1,13})
            {
                if(directories<=index)continue;
                if(size<112+(index+1)*8)throw new InvalidDataException("Native dependency directory exceeds header");
                int record=optional+112+index*8;uint rva=U32(bytes,record),length=U32(bytes,record+4);
                if(rva==0&&length==0)continue;
                int table=Offset(bytes,rva,sections,count,headers),width=index==1?20:32;bool terminated=false;
                Bounds(bytes,table,length);
                for(int n=0;n<4096&&(long)(n+1)*width<=length;n++)
                {
                    int entry=checked(table+n*width);bool empty=true;
                    for(int position=0;position<width;position+=4)if(U32(bytes,entry+position)!=0)empty=false;
                    if(empty) { terminated=true;break; }
                    if(index==13&&U32(bytes,entry)!=1)throw new InvalidDataException("Unsupported native delay dependency address mode");
                    uint nameRva=U32(bytes,entry+(index==1?12:4));int offset=Offset(bytes,nameRva,sections,count,headers),end=offset;
                    while(end<bytes.Length&&end-offset<=260&&bytes[end]!=0)end++;
                    if(end==bytes.Length||end-offset>260)throw new InvalidDataException("Unterminated native dependency name");
                    Dependency(Encoding.ASCII.GetString(bytes,offset,end-offset));
                }
                if(!terminated)throw new InvalidDataException("Unterminated native dependency table");
            }
        }
    }

    internal sealed class FreshProtectionTree : IDisposable
    {
        readonly string root,claim;
        readonly List<SafeFileHandle> ancestors;
        readonly List<SafeFileHandle> ownedObjects=new List<SafeFileHandle>();
        readonly Dictionary<string,BaselineEntry> creationBaseline=new Dictionary<string,BaselineEntry>(StringComparer.Ordinal);
        bool consumed,disposed;
        private FreshProtectionTree(string root,string claim,List<SafeFileHandle> ancestors)
        { this.root=root;this.claim=claim;this.ancestors=ancestors; }
        public string Root { get { return root; } }
        internal bool ConsumedByThisOwner { get { return consumed; } }
        public static FreshProtectionTree Create(string path)
        {
            string root=Native.LocalPath(path,false); string claim=root+".claim";
            var anchors=Native.Anchor(root);
            try
            {
                if(Native.DirectoryExists(root)||Native.FileExists(root)||Native.DirectoryExists(claim)||Native.FileExists(claim))
                    throw new InvalidOperationException("Attempt root or provenance already exists; never reusable");
                // Atomic, permanent claim precedes object creation. A partial
                // preparation is retained and can never become another baseline.
                Native.CreateNewDirectory(claim,true);
                var tree=new FreshProtectionTree(root,claim,anchors);
                try { tree.TrackCreated(root,Native.CreateNewDirectoryHandle(root,false));return tree; }
                catch { tree.Dispose();throw; }
            }
            catch { foreach(var handle in anchors)handle.Dispose(); throw; }
        }
        internal static FreshProtectionTree CreateReserved(string root,string claim)
        {
            var anchors=Native.Anchor(root);
            try
            {
                if(Native.DirectoryExists(root)||Native.FileExists(root)||!Native.DirectoryExists(claim))
                    throw new InvalidOperationException("Reserved materialization is not fresh");
                var tree=new FreshProtectionTree(root,claim,anchors);
                try { tree.TrackCreated(root,Native.CreateNewDirectoryHandle(root,false));return tree; } catch { tree.Dispose();throw; }
            }
            catch { foreach(var handle in anchors)handle.Dispose();throw; }
        }
        internal static FreshProtectionTree OpenPrepared(string root,string claim,BaselineEntry[] baseline,string preparedDigest)
        {
            // This is the only existing-object entrypoint. It accepts a pinned
            // prepared baseline, never captures a replacement baseline.
            if(!Native.DirectoryExists(claim)||!Native.DirectoryExists(Path.Combine(claim,"prepared-"+preparedDigest)))
                throw new InvalidOperationException("Attempt provenance is missing or mismatched");
            if(Native.DirectoryExists(Path.Combine(claim,"consumed")))throw new InvalidOperationException("Attempt already consumed");
            var tree=new FreshProtectionTree(root,claim,Native.Anchor(root));
            try
            {
                var paths=Walk(root);
                if(paths.Count!=baseline.Length)throw new InvalidOperationException("Prepared object inventory mismatch");
                var required=new Dictionary<string,BaselineEntry>(StringComparer.Ordinal);
                foreach(var row in baseline)required.Add(row.RelativePath,row);
                foreach(string path in paths)
                {
                    BaselineEntry expected;
                    if(!required.TryGetValue(Native.Relative(root,path),out expected))
                        throw new InvalidOperationException("Unproven prepared object");
                    tree.Track(path);
                    if(!Same(expected,tree.creationBaseline[expected.RelativePath]))
                        throw new InvalidOperationException("Prepared content/object/security baseline drift");
                }
                return tree;
            }
            catch { tree.Dispose();throw; }
        }
        void Track(string path)
        {
            var handle=Native.Open(path,Native.ReadData|Native.ReadAttributes|Native.ReadControl,3);
            ownedObjects.Add(handle);
            creationBaseline.Add(Native.Relative(root,path),Capture(root,path,handle));
        }
        void TrackCreated(string path,SafeFileHandle handle)
        { ownedObjects.Add(handle);creationBaseline.Add(Native.Relative(root,path),Capture(root,path,handle)); }
        public void CreateDirectory(string relative)
        {
            RequirePreparing();string path=Native.Child(root,relative);
            var missing=new List<string>();string directory=path;
            while(directory!=root&&!creationBaseline.ContainsKey(Native.Relative(root,directory)))
            { missing.Add(directory);directory=Path.GetDirectoryName(directory); }
            missing.Reverse();
            foreach(string item in missing)TrackCreated(item,Native.CreateNewDirectoryHandle(item,false));
        }
        void RequirePreparing()
        {
            if(disposed||consumed||Native.DirectoryExists(Path.Combine(claim,"consumed")))
                throw new InvalidOperationException("Protection attempt has been consumed");
        }
        public void WriteNewFile(string relative,byte[] content)
        {
            RequirePreparing(); string path=Native.Child(root,relative);
            string directory=Path.GetDirectoryName(path);
            if(directory!=root)CreateDirectory(Native.Relative(root,directory));
            string sid=WindowsIdentity.GetCurrent().User.Value;
            // Keep the same creation-time protected DACL and exclusive create,
            // but pass the actual NT-created handle to FileStream. The path/
            // FileSecurity overload applies a legacy MAX_PATH limit even when
            // the canonical local path and its pinned parents are valid.
            using(var created=Native.CreateNewOwnerFileHandle(path,"D:P(A;;FA;;;"+sid+")(A;;FA;;;SY)"))
            using(var stream=new FileStream(created,FileAccess.ReadWrite))
            {
                stream.Write(content,0,content.Length); stream.Flush(true);
                // Retain a read handle before closing the creation handle. The
                // directory/file object can never be replaced under our baseline.
                Track(path);
            }
        }
        internal static List<string> Walk(string root)
        {
            var result=new List<string>(); var queue=new Queue<string>(); queue.Enqueue(root);
            while(queue.Count>0)
            {
                string path=queue.Dequeue();
                using(var handle=Native.Open(path,Native.ReadAttributes,3))
                {
                    var information=Native.Information(handle); result.Add(path);
                    if((information.Attributes&0x10)!=0)
                    {
                        var children=Native.DirectoryEntries(path); Array.Sort(children,StringComparer.Ordinal);
                        foreach(string child in children)queue.Enqueue(child);
                    }
                }
            }
            return result;
        }
        internal static BaselineEntry Capture(string root,string path,SafeFileHandle handle)
        {
            var info=Native.Information(handle); bool directory=(info.Attributes&0x10)!=0;
            byte[] descriptor=Native.Descriptor(handle);
            var raw=new RawSecurityDescriptor(descriptor,0);
            return new BaselineEntry { RelativePath=Native.Relative(root,path), Directory=directory,
                ObjectIdentity=Native.ObjectId(handle),DescriptorIdentity=Native.Hash(descriptor),
                ControlFlags=(int)raw.ControlFlags,ContentSha256=directory?null:Native.HashFile(handle) };
        }
        public BaselineEntry[] CaptureBaseline()
        {
            RequirePreparing(); var entries=new List<BaselineEntry>();var paths=Walk(root);
            if(paths.Count!=creationBaseline.Count)throw new InvalidOperationException("Foreign object in fresh materialization");
            foreach(string path in paths)
            {
                BaselineEntry expected;
                if(!creationBaseline.TryGetValue(Native.Relative(root,path),out expected))
                    throw new InvalidOperationException("Object has no current-process creation provenance");
                using(var handle=Native.Open(path,Native.ReadData|Native.ReadAttributes|Native.ReadControl|Native.WriteDac,1))
                {
                    var entry=Capture(root,path,handle);
                    if(!Same(expected,entry))throw new InvalidOperationException("Creation baseline changed before preparation");
                    if((entry.ControlFlags&(int)ControlFlags.DiscretionaryAclProtected)==0)
                        throw new InvalidOperationException("Fresh baseline requires its creation-time protected DACL");
                    entries.Add(entry);
                }
            }
            return entries.ToArray();
        }
        public bool MatchesBaseline(BaselineEntry[] baseline)
        {
            var paths=Walk(root); if(paths.Count!=baseline.Length)return false;
            var expected=new Dictionary<string,BaselineEntry>(StringComparer.Ordinal);
            foreach(var entry in baseline)expected.Add(entry.RelativePath,entry);
            foreach(string path in paths)
            {
                using(var handle=Native.Open(path,Native.ReadData|Native.ReadAttributes|Native.ReadControl,1))
                {
                    BaselineEntry entry;
                    if(!expected.TryGetValue(Native.Relative(root,path),out entry)||!Same(entry,Capture(root,path,handle)))return false;
                }
            }
            return true;
        }
        internal static bool Same(BaselineEntry a,BaselineEntry b)
        {
            return a.RelativePath==b.RelativePath&&a.Directory==b.Directory&&a.ObjectIdentity==b.ObjectIdentity
                &&a.DescriptorIdentity==b.DescriptorIdentity&&a.ControlFlags==b.ControlFlags&&a.ContentSha256==b.ContentSha256;
        }
        public ProtectedRuntimeTreeV3 Protect(BaselineEntry[] baseline)
        {
            RequirePreparing();
            Native.CreateNewDirectory(Path.Combine(claim,"consumed"),true);
            consumed=true; // Never cleared, even when construction/restoration fails.
            return new ProtectedRuntimeTreeV3(root,baseline);
        }
        public void Dispose()
        {
            if(disposed)return;disposed=true;
            foreach(var handle in ownedObjects)handle.Dispose();
            foreach(var handle in ancestors)handle.Dispose();
        }
    }

    internal sealed class ProtectedRuntimeTreeV3 : IDisposable
    {
        sealed class Entry
        { internal string Path;internal SafeFileHandle Handle;internal byte[] Original;internal BaselineEntry Baseline;internal bool Sealed; }
        readonly List<Entry> entries=new List<Entry>();
        readonly string root;
        bool disposed;
        public bool IsSealed { get; private set; }
        public bool Restored { get; private set; }
        internal ProtectedRuntimeTreeV3(string root,BaselineEntry[] baseline)
        {
            this.root=root;
            try
            {
                var expected=new Dictionary<string,BaselineEntry>(StringComparer.Ordinal);
                foreach(var item in baseline)expected.Add(item.RelativePath,item);
                var paths=FreshProtectionTree.Walk(root);
                if(paths.Count!=expected.Count)throw new InvalidOperationException("Payload membership drift before sealing");
                foreach(string path in paths)
                {
                    var handle=Native.Open(path,Native.ReadData|Native.ReadAttributes|Native.ReadControl|Native.WriteDac,1);
                    var entry=new Entry { Path=path,Handle=handle };entries.Add(entry);
                    entry.Original=Native.Descriptor(handle);
                    BaselineEntry required;
                    var actual=FreshProtectionTree.Capture(root,path,handle);
                    if(!expected.TryGetValue(actual.RelativePath,out required)||!FreshProtectionTree.Same(required,actual))
                        throw new InvalidOperationException("Content/object/security baseline drift before sealing");
                    entry.Baseline=required;
                }
                byte[] sealedDescriptor=Native.FromSddl(Native.SealedDacl);
                foreach(var entry in entries)
                {
                    if(!Native.Equal(entry.Original,Native.Descriptor(entry.Handle)))
                        throw new InvalidOperationException("Security baseline drift immediately before sealing");
                    Native.SetDacl(entry.Handle,sealedDescriptor);entry.Sealed=true;
                }
                IsSealed=true;
                VerifyProtected();
            }
            catch(Exception original)
            {
                try { Dispose(); } catch(Exception cleanup) { throw new AggregateException(original,cleanup); }
                throw;
            }
        }
        public string Sha256(string relative)
        {
            if(!IsSealed||disposed)throw new InvalidOperationException("Protection interval is not active");
            foreach(var entry in entries)
                if(entry.Baseline.RelativePath==relative&&!entry.Baseline.Directory)return Native.HashFile(entry.Handle);
            throw new InvalidOperationException("Unsealed file requested");
        }
        public void VerifyProtected()
        {
            if(!IsSealed||disposed)throw new InvalidOperationException("Protection interval is not active");
            var after=FreshProtectionTree.Walk(root);
            if(after.Count!=entries.Count)throw new InvalidOperationException("Payload addition during sealing");
            foreach(var entry in entries)
            {
                var actual=FreshProtectionTree.Capture(root,entry.Path,entry.Handle);
                if(actual.ObjectIdentity!=entry.Baseline.ObjectIdentity||actual.ContentSha256!=entry.Baseline.ContentSha256||!Native.HasSealedDacl(entry.Handle))
                    throw new InvalidOperationException("Protected content/security verification failed");
            }
        }
        public void Dispose()
        {
            if(disposed)return;disposed=true;var failures=new List<Exception>();
            foreach(var entry in entries)
            {
                try
                {
                    if(entry.Sealed)
                    {
                        Native.SetDacl(entry.Handle,entry.Original);
                        if(!Native.Equal(entry.Original,Native.Descriptor(entry.Handle)))
                            throw new InvalidOperationException("Exact security descriptor restoration not established: "+entry.Path);
                        entry.Sealed=false;
                    }
                }
                catch(Exception error) { failures.Add(error); }
                finally { try { entry.Handle.Dispose(); } catch(Exception error) { failures.Add(error); } }
            }
            IsSealed=false;Restored=failures.Count==0;
            if(failures.Count>0)throw new AggregateException("Protection restoration incomplete",failures);
        }
    }

    internal static class Evidence
    {
        internal static Dictionary<string,object> Obj(object value)
        { var result=value as Dictionary<string,object>;if(result==null)throw new InvalidDataException("Expected evidence object");return result; }
        internal static string Str(object value)
        { var result=value as string;if(result==null)throw new InvalidDataException("Expected evidence string");return result; }
        internal static string Escape(string text)
        {
            var result=new StringBuilder("\"");
            foreach(char value in text)
            {
                switch(value)
                {
                    case '"':result.Append("\\\"");break;case '\\':result.Append("\\\\");break;
                    case '\b':result.Append("\\b");break;case '\f':result.Append("\\f");break;
                    case '\n':result.Append("\\n");break;case '\r':result.Append("\\r");break;case '\t':result.Append("\\t");break;
                    default:if(value<32)result.Append("\\u"+((int)value).ToString("x4"));else result.Append(value);break;
                }
            }
            return result.Append('"').ToString();
        }
        internal static string Canonical(object value)
        {
            if(value==null)return "null";if(value is string)return Escape((string)value);
            if(value is bool)return (bool)value?"true":"false";
            var map=value as Dictionary<string,object>;
            if(map!=null)
            {
                var keys=new List<string>(map.Keys);keys.Sort(StringComparer.Ordinal);var parts=new List<string>();
                foreach(string key in keys)parts.Add(Escape(key)+":"+Canonical(map[key]));return "{"+String.Join(",",parts)+"}";
            }
            if(value is IEnumerable)
            { var parts=new List<string>();foreach(object item in (IEnumerable)value)parts.Add(Canonical(item));return "["+String.Join(",",parts)+"]"; }
            if(value is int||value is long||value is uint||value is ulong||value is decimal)
                return Convert.ToString(value,CultureInfo.InvariantCulture);
            if(value is double)
            {
                double number=(double)value;if(Double.IsNaN(number)||Double.IsInfinity(number))throw new InvalidDataException("Nonfinite evidence number");
                string text=number.ToString("R",CultureInfo.InvariantCulture).ToLowerInvariant();
                return text.IndexOf('.')<0&&text.IndexOf('e')<0?text+".0":text;
            }
            throw new InvalidDataException("Unsupported evidence value");
        }
        internal static string Hash(object value) { return Native.Hash(Encoding.UTF8.GetBytes(Canonical(value))); }
        internal static string Identity(string name,object value) { return name+":sha256:"+Hash(value); }
        internal static Dictionary<string,object> Seal(string name,Dictionary<string,object> body)
        { body.Add("identity",Identity(name,body));return body; }
        internal static void CheckIdentity(Dictionary<string,object> value,string name)
        {
            var copy=new Dictionary<string,object>(value);copy.Remove("identity");
            if(Str(value["identity"])!=Identity(name,copy))throw new InvalidDataException("Evidence identity mismatch: "+name);
        }
        internal static void Closed(Dictionary<string,object> value,params string[] keys)
        {
            var expected=new HashSet<string>(keys,StringComparer.Ordinal);
            if(!expected.SetEquals(value.Keys))throw new InvalidDataException("Evidence field set mismatch");
        }
        internal static void Require(bool value,string message)
        { if(!value)throw new InvalidOperationException(message); }
        internal static void CoverageReference(object value,string space)
        {
            var reference=Obj(value);Closed(reference,"identity","pin");
            Require(Regex.IsMatch(Str(reference["identity"]),"^"+Regex.Escape(space)+":sha256:[0-9a-f]{64}$"),"Coverage reference namespace mismatch");
            var pin=Obj(reference["pin"]);Closed(pin,"byte_length","sha256");
            Require((pin["byte_length"] is int||pin["byte_length"] is long)&&Convert.ToInt64(pin["byte_length"])>0
                &&Convert.ToInt64(pin["byte_length"])<=268435456&&Regex.IsMatch(Str(pin["sha256"]),"^[0-9a-f]{64}$"),"Coverage reference byte pin mismatch");
        }
        internal static void CoverageEnvelope(object value)
        {
            // Structural native enforcement is additional to the fresh protected
            // owner verification; a digest alone is never authentication.
            var envelope=Obj(value);Closed(envelope,"contract_version","core","receipt","execution_authorized");
            var core=Obj(envelope["core"]);Closed(core,"contract_version","result","binding","candidate","history","lineage","expires_utc","execution_authorized");
            var history=Obj(core["history"]);Closed(history,"contract_version","historical_claim","historical_equal","historical_cause","prior_outcome","scope","coverage","known_recorded_fields","current_window");
            var coverage=Obj(history["coverage"]);Closed(coverage,"scope","measurement","baseline","assessment","historical_coverage","historical_object_continuity","hidden_contents");
            bool typedRoots=Str(Obj(coverage["scope"])["identity"]).StartsWith("historical-coverage-scope:v3:sha256:",StringComparison.Ordinal);
            CoverageReference(coverage["scope"],typedRoots?"historical-coverage-scope:v3":"historical-coverage-scope:v2");
            CoverageReference(coverage["measurement"],typedRoots?"policy-coverage-measurement:v3":"policy-coverage-measurement:v2");
            CoverageReference(coverage["baseline"],"policy-coverage-inventory:v2");
            CoverageReference(coverage["assessment"],"policy-coverage-assessment:v2");
            CoverageReference(core["result"],"policy-qualified-execution-result:v2");
            var lineage=Obj(core["lineage"]);var binding=Obj(core["binding"]);var receipt=Obj(envelope["receipt"]);
            Closed(receipt,"contract_version","core","pre_result_closure_identity","authority_state_reference","recorded_at_utc","execution_authorized","receipt_identity");
            Require(Str(envelope["contract_version"])=="policy-qualified-downstream-envelope-v2"
                &&Str(core["contract_version"])=="policy-qualified-result-binding-core-v2"
                &&Str(history["contract_version"])=="policy-coverage-historical-claim-v2"
                &&Str(history["historical_claim"])=="UNRESOLVED_RAW_DIFFERENCE"&&!(bool)history["historical_equal"]
                &&Str(history["historical_cause"])=="UNRESOLVED"&&Str(history["prior_outcome"])=="BLOCKED"
                &&Str(history["known_recorded_fields"])=="MATCH"
                &&Str(history["current_window"])=="STRICT_COMPLETE_PRESERVATION_WITHIN_DECLARED_SCOPE"
                &&Str(coverage["historical_coverage"])=="UNKNOWN"
                &&Str(coverage["historical_object_continuity"])=="NOT_ESTABLISHED"
                &&Str(coverage["hidden_contents"])=="UNKNOWN_NOT_RETROACTIVELY_COMPLETED"
                &&Str(lineage["contract_version"])=="historical-state-qualification-lineage-v2"
                &&Canonical(lineage["coverage"])==Canonical(coverage)
                &&Object.Equals(binding["lineage_identity"],lineage["lineage_identity"])
                &&Canonical(core["candidate"])==Canonical(lineage["candidate"])
                &&Regex.IsMatch(Str(binding["acceptance_policy_identity"]),"^historical-state-acceptance-policy:v2:sha256:[0-9a-f]{64}$")
                &&Regex.IsMatch(Str(binding["approval_identity"]),"^historical-state-operator-approval:v2:sha256:[0-9a-f]{64}$")
                &&Str(receipt["contract_version"])=="policy-qualified-acceptance-receipt-v2"
                &&Regex.IsMatch(Str(receipt["receipt_identity"]),"^policy-qualified-acceptance-receipt:v2:sha256:[0-9a-f]{64}$")
                &&Canonical(receipt["core"])==Canonical(core)
                &&!(bool)envelope["execution_authorized"]&&!(bool)core["execution_authorized"]&&!(bool)receipt["execution_authorized"],
                "Coverage consumer lost version, irreversible baseline, unknown history or authority limits");
        }
        internal static void WriteNew(string path,Dictionary<string,object> value)
        {
            string sid=WindowsIdentity.GetCurrent().User.Value;
            using(var created=Native.CreateNewOwnerFileHandle(path,"D:P(A;;FA;;;"+sid+")(A;;FA;;;SY)",true))
            using(var stream=new FileStream(created,FileAccess.ReadWrite))
            {
                byte[] bytes=Encoding.UTF8.GetBytes(Canonical(value));stream.Write(bytes,0,bytes.Length);stream.Flush(true);
                // Permanent new evidence, not a restoration journal. No original
                // descriptor or reopened ACL-reset authority is persisted.
                Native.SetDacl(stream.SafeFileHandle,Native.FromSddl(Native.SealedDacl));
            }
        }
        internal static Dictionary<string,object> Baseline(BaselineEntry entry)
        {
            return new Dictionary<string,object> { {"relative_path",entry.RelativePath},{"directory",entry.Directory},
                {"object_identity",entry.ObjectIdentity},{"descriptor_identity",entry.DescriptorIdentity},
                {"control_flags",entry.ControlFlags},{"content_sha256",entry.ContentSha256} };
        }
        internal static BaselineEntry[] Baselines(object value)
        {
            var result=new List<BaselineEntry>();foreach(object item in (IEnumerable)value)
            {
                var row=Obj(item);Closed(row,"relative_path","directory","object_identity","descriptor_identity","control_flags","content_sha256");
                result.Add(new BaselineEntry { RelativePath=Str(row["relative_path"]),Directory=(bool)row["directory"],
                    ObjectIdentity=Str(row["object_identity"]),DescriptorIdentity=Str(row["descriptor_identity"]),
                    ControlFlags=Convert.ToInt32(row["control_flags"]),ContentSha256=row["content_sha256"]==null?null:Str(row["content_sha256"]) });
            }
            return result.ToArray();
        }
    }

    internal sealed class PinnedJson : IDisposable
    {
        readonly List<SafeFileHandle> ancestors;
        readonly FileStream stream;
        internal readonly Dictionary<string,object> Value;
        internal PinnedJson(string path,string expectedHash)
        {
            Native.LocalPath(path,false);ancestors=Native.Anchor(path);
            try
            {
                var leaf=Native.Open(path,Native.ReadData|Native.ReadAttributes,1);
                try { stream=new FileStream(leaf,FileAccess.Read); } catch { leaf.Dispose();throw; }
                Native.Information(stream.SafeFileHandle);
                if(stream.Length>64*1024*1024)throw new InvalidDataException("Evidence exceeds size bound");
                byte[] bytes=new byte[stream.Length];int offset=0;
                while(offset<bytes.Length) { int count=stream.Read(bytes,offset,bytes.Length-offset);if(count==0)throw new EndOfStreamException();offset+=count; }
                if(expectedHash!=null&&Native.Hash(bytes)!=expectedHash)throw new InvalidDataException("Pinned evidence bytes changed");
                string text=new UTF8Encoding(false,true).GetString(bytes);
                Value=Evidence.Obj(new JavaScriptSerializer { MaxJsonLength=64*1024*1024,RecursionLimit=128 }.DeserializeObject(text));
                if(Evidence.Canonical(Value)!=text)throw new InvalidDataException("Evidence must be canonical, without duplicate keys");
            }
            catch { Dispose();throw; }
        }
        public void Dispose()
        { if(stream!=null)stream.Dispose();foreach(var handle in ancestors)handle.Dispose(); }
    }

    internal sealed class AncestorPins : IDisposable
    {
        readonly List<SafeFileHandle> handles;
        internal AncestorPins(string path) { handles=Native.Anchor(path); }
        public void Dispose() { foreach(var handle in handles)handle.Dispose(); }
    }

    internal sealed class ProtectedChildren : IDisposable
    {
        [StructLayout(LayoutKind.Sequential)] struct BasicLimits
        { internal long PerProcess,PerJob;internal uint Flags;internal UIntPtr Minimum,Maximum;internal uint ActiveLimit;internal UIntPtr Affinity;internal uint Priority,Scheduling; }
        [StructLayout(LayoutKind.Sequential)] struct IoCounters
        { internal ulong ReadOperations,WriteOperations,OtherOperations,ReadBytes,WriteBytes,OtherBytes; }
        [StructLayout(LayoutKind.Sequential)] struct ExtendedLimits
        { internal BasicLimits Basic;internal IoCounters Io;internal UIntPtr ProcessMemory,JobMemory,PeakProcessMemory,PeakJobMemory; }
        [StructLayout(LayoutKind.Sequential)] struct Accounting
        { internal long UserTime,KernelTime,PeriodUserTime,PeriodKernelTime;internal uint PageFaults,TotalProcesses,ActiveProcesses,TerminatedProcesses; }
        [StructLayout(LayoutKind.Sequential,CharSet=CharSet.Unicode)] struct Startup
        {
            internal int Size;internal string Reserved,Desktop,Title;internal uint X,Y,XSize,YSize,XCount,YCount,Fill,Flags;
            internal ushort Show,ReservedSize;internal IntPtr ReservedBytes,Input,Output,Error;
        }
        [StructLayout(LayoutKind.Sequential)] struct ProcessInformation
        { internal IntPtr Process,Thread;internal uint ProcessId,ThreadId; }
        [StructLayout(LayoutKind.Sequential)] struct ProcessBasic
        { internal IntPtr Reserved1,Peb,Reserved2,Reserved3,ProcessId,ParentId; }
        [DllImport("kernel32.dll",CharSet=CharSet.Unicode,SetLastError=true)] static extern IntPtr CreateJobObjectW(IntPtr security,string name);
        [DllImport("kernel32.dll",SetLastError=true)] static extern bool SetInformationJobObject(IntPtr job,int kind,ref ExtendedLimits value,int length);
        [DllImport("kernel32.dll",SetLastError=true)] static extern bool QueryInformationJobObject(IntPtr job,int kind,out Accounting value,int length,IntPtr returned);
        [DllImport("kernel32.dll",SetLastError=true)] static extern bool AssignProcessToJobObject(IntPtr job,IntPtr process);
        [DllImport("kernel32.dll",SetLastError=true)] static extern bool IsProcessInJob(IntPtr process,IntPtr job,out bool result);
        [DllImport("kernel32.dll",SetLastError=true)] static extern bool TerminateJobObject(IntPtr job,uint code);
        [DllImport("kernel32.dll",SetLastError=true)] static extern bool TerminateProcess(IntPtr process,uint code);
        [DllImport("kernel32.dll",SetLastError=true)] static extern uint ResumeThread(IntPtr thread);
        [DllImport("kernel32.dll",SetLastError=true)] static extern bool GetExitCodeProcess(IntPtr process,out uint code);
        [DllImport("kernel32.dll",SetLastError=true)] static extern uint WaitForSingleObject(IntPtr handle,uint milliseconds);
        [DllImport("kernel32.dll")] static extern bool CloseHandle(IntPtr handle);
        [DllImport("kernel32.dll")] static extern IntPtr GetStdHandle(int kind);
        [StructLayout(LayoutKind.Sequential)] struct PrivilegeId { internal uint Low;internal int High; }
        [DllImport("advapi32.dll",CharSet=CharSet.Unicode,SetLastError=true)] static extern bool LookupPrivilegeValueW(string system,string name,out PrivilegeId value);
        [StructLayout(LayoutKind.Sequential)] struct PipeAttributes { internal int Length;internal IntPtr Descriptor;internal int Inherit; }
        [DllImport("kernel32.dll",SetLastError=true)] static extern bool CreatePipe(out SafeFileHandle read,out SafeFileHandle write,ref PipeAttributes security,uint size);
        [DllImport("kernel32.dll",SetLastError=true)] static extern bool SetHandleInformation(SafeFileHandle handle,uint mask,uint flags);
        [DllImport("kernel32.dll",SetLastError=true)] static extern bool DuplicateHandle(IntPtr sourceProcess,IntPtr source,IntPtr targetProcess,
            out IntPtr target,uint access,bool inherit,uint options);
        [DllImport("kernel32.dll",CharSet=CharSet.Unicode,SetLastError=true)]
        static extern bool CreateProcessW(string application,StringBuilder command,IntPtr processSecurity,IntPtr threadSecurity,
            bool inherit,uint flags,IntPtr environment,string cwd,ref Startup startup,out ProcessInformation information);
        [StructLayout(LayoutKind.Sequential)] struct SidAndAttributes
        { internal IntPtr Sid;internal uint Attributes; }
        [DllImport("advapi32.dll",SetLastError=true)]
        static extern bool OpenProcessToken(IntPtr process,uint access,out SafeAccessTokenHandle token);
        [DllImport("advapi32.dll",SetLastError=true)]
        static extern bool CreateRestrictedToken(SafeAccessTokenHandle original,uint flags,uint disabledCount,ref SidAndAttributes disabled,
            uint deletedPrivileges,IntPtr privileges,uint restrictedCount,IntPtr restricted,out SafeAccessTokenHandle token);
        [DllImport("advapi32.dll",SetLastError=true)]
        static extern bool SetTokenInformation(SafeAccessTokenHandle token,int kind,IntPtr value,int length);
        [DllImport("advapi32.dll",SetLastError=true)]
        static extern bool GetTokenInformation(SafeAccessTokenHandle token,int kind,IntPtr value,uint length,out uint needed);
        [DllImport("advapi32.dll",SetLastError=true)]
        static extern bool IsTokenRestricted(SafeAccessTokenHandle token);
        [DllImport("advapi32.dll",CharSet=CharSet.Unicode,SetLastError=true)]
        static extern bool CreateProcessAsUserW(SafeAccessTokenHandle token,string application,StringBuilder command,IntPtr processSecurity,IntPtr threadSecurity,
            bool inherit,uint flags,IntPtr environment,string cwd,ref Startup startup,out ProcessInformation information);
        [DllImport("ntdll.dll")] static extern int NtQueryInformationProcess(IntPtr process,int kind,out ProcessBasic value,int size,out int returned);
        IntPtr job;
        readonly object jobLock=new object();
        bool terminated;
        readonly Dictionary<int,IntPtr> processHandles=new Dictionary<int,IntPtr>();
        static IntPtr InheritableStandardHandle(int kind,List<IntPtr> owned)
        {
            IntPtr source=GetStdHandle(kind),copy;
            if(source==IntPtr.Zero||source==new IntPtr(-1))return source;
            if(!DuplicateHandle(new IntPtr(-1),source,new IntPtr(-1),out copy,0,true,2))throw new Win32Exception(Marshal.GetLastWin32Error());
            owned.Add(copy);return copy;
        }
        internal ProtectedChildren(long maximumMemoryBytes=0)
        {
            job=CreateJobObjectW(IntPtr.Zero,null);
            if(job==IntPtr.Zero)throw new Win32Exception(Marshal.GetLastWin32Error());
            var limits=new ExtendedLimits();limits.Basic.Flags=0x2000; // KILL_ON_JOB_CLOSE, no breakaway permission.
            if(maximumMemoryBytes!=0)
            {
                if(maximumMemoryBytes<64L*1024*1024||maximumMemoryBytes>8L*1024*1024*1024)
                {Dispose();throw new InvalidDataException("Invalid finite worker memory bound");}
                limits.Basic.Flags|=0x300; // PROCESS_MEMORY and JOB_MEMORY; default callers unchanged.
                limits.ProcessMemory=new UIntPtr((ulong)maximumMemoryBytes);
                limits.JobMemory=new UIntPtr((ulong)maximumMemoryBytes);
            }
            if(!SetInformationJobObject(job,9,ref limits,Marshal.SizeOf(typeof(ExtendedLimits))))
            { int error=Marshal.GetLastWin32Error();Dispose();throw new Win32Exception(error); }
        }
        internal static SafeAccessTokenHandle ConditionalChildToken()
        {
            SafeAccessTokenHandle original;
            // CreateRestrictedToken returns the same handle access mask as its
            // input. ADJUST_DEFAULT is needed to lower the NEW token's integrity;
            // ASSIGN_PRIMARY is needed to create its suspended process.
            if(!OpenProcessToken(new IntPtr(-1),0x008b,out original))throw new Win32Exception(Marshal.GetLastWin32Error(),"Cannot open current token for restricted duplication");
            using(original)
            {
                var administrator=new SecurityIdentifier("S-1-5-32-544");
                byte[] adminBytes=new byte[administrator.BinaryLength];administrator.GetBinaryForm(adminBytes,0);
                var adminPin=GCHandle.Alloc(adminBytes,GCHandleType.Pinned);
                SafeAccessTokenHandle token=null;
                try
                {
                    var disabled=new SidAndAttributes { Sid=adminPin.AddrOfPinnedObject() };
                    // The owner is high integrity, the child is medium and has
                    // administrator membership and privileged operations removed.
                    // Durable state belongs to the distinct service SID, SYSTEM
                    // and administrators, with only read access for this user.
                    // No existing desktop or host-wide security policy is changed.
                    if(!Native.TokenHasEnabledSid(original,"S-1-5-32-544"))throw new InvalidOperationException("Conditional launch requires the existing elevated native owner");
                    RequireIntegrity(original,"S-1-16-12288");
                    if(!CreateRestrictedToken(original,1u|4u,1,ref disabled,0,IntPtr.Zero,0,IntPtr.Zero,out token))
                        throw new Win32Exception(Marshal.GetLastWin32Error(),"Cannot create limited child token");
                    var medium=new SecurityIdentifier("S-1-16-8192");byte[] sid=new byte[medium.BinaryLength];medium.GetBinaryForm(sid,0);
                    int size=Marshal.SizeOf(typeof(SidAndAttributes));IntPtr label=Marshal.AllocHGlobal(size+sid.Length);
                    try
                    {
                        IntPtr pointer=IntPtr.Add(label,size);Marshal.Copy(sid,0,pointer,sid.Length);
                        Marshal.StructureToPtr(new SidAndAttributes { Sid=pointer,Attributes=0x20 },label,false);
                        if(!SetTokenInformation(token,25,label,size+sid.Length))throw new Win32Exception(Marshal.GetLastWin32Error(),"Cannot lower restricted child integrity");
                    }
                    finally { Marshal.FreeHGlobal(label); }
                    // Filtering retains the elevated token's default DACL. On
                    // this host it grants BA/SYSTEM full access but the user only
                    // read/execute via the logon SID. Give the NEW medium child
                    // access to its own newly created process/thread objects.
                    // This changes no owner process, store, or existing object.
                    string user=WindowsIdentity.GetCurrent().User.Value;
                    var defaults=new RawSecurityDescriptor("D:P(A;;GA;;;"+user+")(A;;GA;;;SY)(A;;GA;;;BA)");
                    byte[] acl=new byte[defaults.DiscretionaryAcl.BinaryLength];defaults.DiscretionaryAcl.GetBinaryForm(acl,0);
                    var aclPin=GCHandle.Alloc(acl,GCHandleType.Pinned);IntPtr defaultDacl=Marshal.AllocHGlobal(IntPtr.Size);
                    try
                    {
                        Marshal.WriteIntPtr(defaultDacl,aclPin.AddrOfPinnedObject());
                        if(!SetTokenInformation(token,6,defaultDacl,IntPtr.Size))throw new Win32Exception(Marshal.GetLastWin32Error(),"Cannot set fresh child object permissions");
                    }
                    finally { Marshal.FreeHGlobal(defaultDacl);aclPin.Free(); }
                    return token;
                }
                catch { if(token!=null)token.Dispose();throw; }
                finally { adminPin.Free(); }
            }
        }
        static void VerifyConditionalChild(IntPtr process)
        {
            SafeAccessTokenHandle token;
            if(!OpenProcessToken(process,8,out token))throw new Win32Exception(Marshal.GetLastWin32Error());
            using(token)
            using(var identity=new WindowsIdentity(token.DangerousGetHandle()))
            {
                if(Native.TokenHasEnabledSid(token,"S-1-5-32-544")
                    ||identity.User.Value!=WindowsIdentity.GetCurrent().User.Value)
                    throw new InvalidOperationException("Conditional child inherited owner/administrator authority");
                RequireIntegrity(token,"S-1-16-8192");
                uint needed;GetTokenInformation(token,3,IntPtr.Zero,0,out needed);
                if(needed==0||needed>4096)throw new InvalidOperationException("Child privilege information unavailable");
                IntPtr privileges=Marshal.AllocHGlobal((int)needed);
                try
                {
                    if(!GetTokenInformation(token,3,privileges,needed,out needed))throw new Win32Exception(Marshal.GetLastWin32Error());
                    // Resolve the local LUID; numerical privilege identifiers
                    // are not an authorization contract.
                    PrivilegeId permitted;
                    if(!LookupPrivilegeValueW(null,"SeChangeNotifyPrivilege",out permitted))throw new Win32Exception(Marshal.GetLastWin32Error());
                    int count=Marshal.ReadInt32(privileges);
                    if(count>1||count<0||(count==1&&(unchecked((uint)Marshal.ReadInt32(privileges,4))!=permitted.Low||Marshal.ReadInt32(privileges,8)!=permitted.High)))
                        throw new InvalidOperationException("Conditional child retained privileged operations");
                }
                finally { Marshal.FreeHGlobal(privileges); }
            }
        }
        static void RequireIntegrity(SafeAccessTokenHandle token,string expected)
        {
                uint needed;GetTokenInformation(token,25,IntPtr.Zero,0,out needed);
                if(needed==0||needed>4096)throw new InvalidOperationException("Conditional child integrity is unavailable");
                IntPtr value=Marshal.AllocHGlobal((int)needed);
                try
                {
                    if(!GetTokenInformation(token,25,value,needed,out needed))throw new Win32Exception(Marshal.GetLastWin32Error());
                    if(new SecurityIdentifier(Marshal.ReadIntPtr(value)).Value!=expected)throw new InvalidOperationException("Native owner/child integrity boundary mismatch");
                }
                finally { Marshal.FreeHGlobal(value); }
        }
        internal Process Start(ProcessStartInfo info,string quotedExecutable,bool conditional=false,Action beforeResume=null,IntPtr[] redirectedHandles=null)
        {
            var names=new List<string>();foreach(string name in info.EnvironmentVariables.Keys)names.Add(name);
            names.Sort(StringComparer.OrdinalIgnoreCase);var block=new StringBuilder();
            foreach(string name in names)block.Append(name).Append('=').Append(info.EnvironmentVariables[name]).Append('\0');
            block.Append('\0');IntPtr environment=Marshal.StringToHGlobalUni(block.ToString());
            var standardHandles=new List<IntPtr>();
            var startup=new Startup { Size=Marshal.SizeOf(typeof(Startup)),Flags=0x100 };
            ProcessInformation created=new ProcessInformation();Process result=null;
            try
            {
                if(redirectedHandles!=null)
                {
                    if(redirectedHandles.Length!=3)throw new InvalidDataException("Three explicit child standard handles required");
                    startup.Input=redirectedHandles[0];startup.Output=redirectedHandles[1];startup.Error=redirectedHandles[2];
                }
                else
                {
                    if(info.RedirectStandardInput||info.RedirectStandardOutput||info.RedirectStandardError)throw new InvalidDataException("Redirection requires explicit protected pipe handles");
                    startup.Input=InheritableStandardHandle(-10,standardHandles);
                    startup.Output=InheritableStandardHandle(-11,standardHandles);
                    startup.Error=InheritableStandardHandle(-12,standardHandles);
                }
                // Suspension closes the redirector-spawn/assignment race. Every
                // interpreter descendant enters this attempt's job before any
                // application code runs, and dies if this owner disappears.
                uint flags=4u|0x400u|(info.CreateNoWindow?0x08000000u:0u);
                if(conditional)
                {
                    using(var token=ConditionalChildToken())
                        if(!CreateProcessAsUserW(token,info.FileName,new StringBuilder(quotedExecutable+" "+info.Arguments),IntPtr.Zero,IntPtr.Zero,true,
                            flags,environment,info.WorkingDirectory,ref startup,out created))throw new Win32Exception(Marshal.GetLastWin32Error());
                    VerifyConditionalChild(created.Process);
                }
                else if(!CreateProcessW(info.FileName,new StringBuilder(quotedExecutable+" "+info.Arguments),IntPtr.Zero,IntPtr.Zero,true,
                    flags,environment,info.WorkingDirectory,ref startup,out created))throw new Win32Exception(Marshal.GetLastWin32Error());
                lock(jobLock)
                {
                    if(terminated||job==IntPtr.Zero)throw new InvalidOperationException("Protection lifetime was cancelled before assignment");
                    if(!AssignProcessToJobObject(job,created.Process))throw new Win32Exception(Marshal.GetLastWin32Error(),"Cannot assign fresh child to protection lifetime");
                }
                result=Process.GetProcessById((int)created.ProcessId);
                if(beforeResume!=null)beforeResume();
                lock(jobLock)
                {
                    if(terminated||job==IntPtr.Zero)throw new InvalidOperationException("Protection lifetime was cancelled before resume");
                    if(ResumeThread(created.Thread)==UInt32.MaxValue)throw new Win32Exception(Marshal.GetLastWin32Error());
                }
                processHandles.Add(result.Id,created.Process);created.Process=IntPtr.Zero;
                return result;
            }
            catch
            {
                if(created.Process!=IntPtr.Zero)TerminateProcess(created.Process,1);
                if(result!=null)result.Dispose();throw;
            }
            finally
            {
                if(created.Thread!=IntPtr.Zero)CloseHandle(created.Thread);
                if(created.Process!=IntPtr.Zero)CloseHandle(created.Process);
                foreach(var handle in standardHandles)CloseHandle(handle);
                Marshal.FreeHGlobal(environment);
            }
        }
        internal Process ValidateClient(uint processId,Process launcher,string interpreter)
        {
            var actual=Process.GetProcessById((int)processId);
            try
            {
                bool inJob;
                if(!IsProcessInJob(actual.Handle,job,out inJob)||!inJob)throw new InvalidOperationException("Phase client escaped its current protection owner");
                if(actual.Id!=launcher.Id)
                {
                    ProcessBasic basic;int returned;
                    if(NtQueryInformationProcess(actual.Handle,0,out basic,Marshal.SizeOf(typeof(ProcessBasic)),out returned)!=0
                        ||basic.ParentId.ToInt64()!=launcher.Id||!String.Equals(actual.MainModule.FileName,interpreter,StringComparison.OrdinalIgnoreCase))
                        throw new InvalidOperationException("Unproven private-interpreter redirector ancestry");
                }
                return actual;
            }
            catch { actual.Dispose();throw; }
        }
        internal bool Quiesce()
        {
            Accounting value;
            if(!QueryInformationJobObject(job,1,out value,Marshal.SizeOf(typeof(Accounting)),IntPtr.Zero))throw new Win32Exception(Marshal.GetLastWin32Error());
            if(value.ActiveProcesses==0)return true;
            if(!TerminateJobObject(job,1))throw new Win32Exception(Marshal.GetLastWin32Error());
            var timer=Stopwatch.StartNew();
            do
            {
                if(!QueryInformationJobObject(job,1,out value,Marshal.SizeOf(typeof(Accounting)),IntPtr.Zero))throw new Win32Exception(Marshal.GetLastWin32Error());
                if(value.ActiveProcesses==0)return true;
                System.Threading.Thread.Sleep(10);
            } while(timer.ElapsedMilliseconds<10000);
            return false;
        }
        internal void Terminate()
        {
            lock(jobLock)
            {
                terminated=true;
                if(job!=IntPtr.Zero&&!TerminateJobObject(job,1))throw new Win32Exception(Marshal.GetLastWin32Error());
            }
        }
        static byte[] ReadBounded(Stream stream,int maximum)
        {
            using(var memory=new MemoryStream())
            {
                byte[] buffer=new byte[65536];int count;
                while((count=stream.Read(buffer,0,buffer.Length))!=0)
                {
                    if(memory.Length+count>maximum)throw new InvalidDataException("Protected worker output exceeds bound");
                    memory.Write(buffer,0,count);
                }
                return memory.ToArray();
            }
        }
        internal byte[] RunRedirected(ProcessStartInfo info,string quotedExecutable,byte[] request,int milliseconds)
        {
            if(request.Length>64*1024*1024)throw new InvalidDataException("Protected worker input exceeds bound");
            var handles=new List<SafeFileHandle>();var streams=new List<Stream>();
            try
            {
                var security=new PipeAttributes { Length=Marshal.SizeOf(typeof(PipeAttributes)),Inherit=1 };
                var childHandles=new IntPtr[3];var childOwned=new List<SafeFileHandle>();
                for(int index=0;index<3;index++)
                {
                    SafeFileHandle read,write;
                    if(!CreatePipe(out read,out write,ref security,65536))throw new Win32Exception(Marshal.GetLastWin32Error());
                    handles.Add(read);handles.Add(write);
                    var parent=index==0?write:read;var child=index==0?read:write;
                    if(!SetHandleInformation(parent,1,0))throw new Win32Exception(Marshal.GetLastWin32Error());
                    streams.Add(new FileStream(parent,index==0?FileAccess.Write:FileAccess.Read,4096,false));
                    childHandles[index]=child.DangerousGetHandle();childOwned.Add(child);
                }
                using(var deadline=new ChildDeadline(this,milliseconds,null))
                using(var child=Start(info,quotedExecutable,false,delegate { deadline.Check(); },childHandles))
                {
                    foreach(var handle in childOwned)handle.Dispose();
                    var input=Task.Run(delegate { try { streams[0].Write(request,0,request.Length); } finally { streams[0].Dispose(); } });
                    var output=Task.Run(delegate { return ReadBounded(streams[1],64*1024*1024); });
                    var errors=Task.Run(delegate { return ReadBounded(streams[2],1024*1024); });
                    if(!Task.WaitAll(new Task[]{input,output,errors},deadline.Remaining))throw new System.TimeoutException("Protected worker exceeded its complete I/O deadline");
                    uint waited=WaitForSingleObject(processHandles[child.Id],(uint)deadline.Remaining);
                    if(waited==0xffffffff)throw new Win32Exception(Marshal.GetLastWin32Error());
                    if(waited!=0)throw new System.TimeoutException("Protected worker exceeded its exit deadline");
                    deadline.Check();
                    if(ExitCode(child)!=0)throw new InvalidDataException("Registered verifier rejected request: "+new UTF8Encoding(false,true).GetString(errors.Result));
                    return output.Result;
                }
            }
            finally
            {
                // The worker and every descendant must stop before any caller
                // accepts output or permits a later owner request to mutate state.
                try { if(!Quiesce())throw new InvalidOperationException("Protected worker descendants survived"); }
                finally { foreach(var stream in streams)stream.Dispose();foreach(var handle in handles)handle.Dispose(); }
            }
        }
        internal uint ExitCode(Process child)
        {
            uint code;if(!GetExitCodeProcess(processHandles[child.Id],out code))throw new Win32Exception(Marshal.GetLastWin32Error());
            return code;
        }
        public void Dispose()
        {
            lock(jobLock)if(job!=IntPtr.Zero) { CloseHandle(job);job=IntPtr.Zero; }
            foreach(var handle in processHandles.Values)CloseHandle(handle);processHandles.Clear();
        }
    }

    internal sealed class ChildDeadline : IDisposable
    {
        readonly Stopwatch elapsed=Stopwatch.StartNew();
        readonly int maximum;
        readonly System.Threading.Timer timer;
        volatile bool expired;
        Exception terminationError;
        internal ChildDeadline(ProtectedChildren children,int milliseconds,Action cancelIo)
        {
            if(milliseconds<1)throw new InvalidDataException("Positive child deadline required");maximum=milliseconds;
            timer=new System.Threading.Timer(delegate {
                expired=true;
                try { children.Terminate(); } catch(Exception error) { terminationError=error; }
                finally { if(cancelIo!=null)try { cancelIo(); } catch(ObjectDisposedException) { } catch(Exception error) { terminationError=error; } }
            },null,milliseconds,System.Threading.Timeout.Infinite);
        }
        internal int Remaining { get { Check();return Math.Max(1,(int)(maximum-elapsed.ElapsedMilliseconds)); } }
        internal void Check()
        {
            if(expired||elapsed.ElapsedMilliseconds>=maximum)throw new System.TimeoutException("Registered child lifetime expired",terminationError);
        }
        public void Dispose()
        {
            using(var complete=new System.Threading.ManualResetEvent(false))if(timer.Dispose(complete))complete.WaitOne();
        }
    }

    // Supplied only by the new native entrypoint after protected Windows owner
    // registration is verified. Serialized inputs cannot supply this interface.
    internal interface IConditionalProtectionOwner
    {
        Dictionary<string,object> Binding { get; }
        void VerifyAttempt(Dictionary<string,object> attempt);
        void VerifyMaterialization(Dictionary<string,object> plan);
        void RequireInertLaunch(Dictionary<string,object> prepared,Dictionary<string,object> packet);
        int MaximumChildSeconds { get; }
    }

    public static class RuntimeProtectionHost
    {
#if !OWNER_ADMISSION_V2
        const string Contract="runtime-protection-attempt-v1";
        static readonly string[] ForbiddenEnvironment={"PYTHONHOME","PYTHONPATH","PYTHONPYCACHEPREFIX","PYTHONUSERBASE","PYTHONSTARTUP",
            "PYTHONINSPECT","PYTHONEXECUTABLE","PYTHONPLATLIBDIR","__PYVENV_LAUNCHER__","_PYTHON_HOST_PLATFORM","LD_LIBRARY_PATH",
            "LD_PRELOAD","DYLD_LIBRARY_PATH","DYLD_INSERT_LIBRARIES","DLL_DIRECTORY","PYTHON_DLL_PATH","QT_PLUGIN_PATH",
            "OPENSSL_MODULES","OPENSSL_ENGINES","OPENSSL_CONF","TRADINGAGENTS_PROTECTION_CONTEXT"};
        static string S(Dictionary<string,object> value,string key) { return Evidence.Str(value[key]); }
        static Dictionary<string,object> O(Dictionary<string,object> value,string key) { return Evidence.Obj(value[key]); }
        static void Require(bool value,string message) { Evidence.Require(value,message); }
        static string SourceIdentity()
        {
            foreach(AssemblyMetadataAttribute attribute in Assembly.GetExecutingAssembly().GetCustomAttributes(typeof(AssemblyMetadataAttribute),false))
                if(attribute.Key=="RuntimeProtectionSourceSha256"&&Regex.IsMatch(attribute.Value,"^[0-9a-f]{64}$"))
                    return "runtime-protection-source:v1:sha256:"+attribute.Value;
            throw new InvalidOperationException("Host source/build identity is absent");
        }
        static void ValidateLiveApplication(Dictionary<string,object> prepared)
        {
            var required=new Dictionary<string,string> {
                {"runtime/app/cli/main.py","RuntimeProtectionCliSha256"},
                {"runtime/app/tradingagents/runtime_protection.py","RuntimeProtectionContractSha256"},
                {"runtime/app/tradingagents/runtime_protection_handoff.py","RuntimeProtectionHandoffSha256"} };
            var metadata=new Dictionary<string,string>();
            foreach(AssemblyMetadataAttribute attribute in Assembly.GetExecutingAssembly().GetCustomAttributes(typeof(AssemblyMetadataAttribute),false))
                metadata.Add(attribute.Key,attribute.Value);
            foreach(var binding in required)
            {
                Require(metadata.ContainsKey(binding.Value)&&Regex.IsMatch(metadata[binding.Value],"^[0-9a-f]{64}$"),"Live application build binding is missing");
                bool found=false;
                foreach(var entry in Evidence.Baselines(prepared["baseline"]))
                    if(entry.RelativePath==binding.Key&&entry.ContentSha256==metadata[binding.Value])found=true;
                Require(found,"Live application lacks the build-bound Step-4 gate: "+binding.Key);
            }
        }
        static bool IsCoverageAttempt(Dictionary<string,object> attempt)
        { return S(attempt,"contract_version")=="policy-qualified-runtime-protection-attempt-v2"; }
        static string ConditionalKind(string stem,Dictionary<string,object> attempt,bool identity)
        {
            string version=S(attempt,"contract_version");
            Require(version=="policy-qualified-runtime-protection-attempt-v1"||version=="policy-qualified-runtime-protection-attempt-v2","Unknown conditional attempt version");
            return "policy-qualified-runtime-protection-"+stem+(identity?":v":"-v")+(IsCoverageAttempt(attempt)?"3":"2");
        }
        static Dictionary<string,object> ValidateAttempt(Dictionary<string,object> attempt,IConditionalProtectionOwner owner=null)
        {
            Evidence.Closed(attempt,"contract_version","inputs","attempt_identity","paths","execution_authorized");
            var inputs=O(attempt,"inputs");
            var inputKeys=new List<string>(new string[]{"contract_version","namespace_root","namespace_identity","source_commit","source_tree","qualified_source_identity",
                "production_preparation_identity","preparation_lineage_identity","completed_session","prospective_thread","prospective_run",
                "runtime_content_identity","app_content_identity","runtime_ancestry_identity","app_ancestry_identity",
                "deployment_configuration_identity","environment_identity","protection_source_identity","attempt_ordinal"});
            if(owner!=null)inputKeys.Add("acceptance");
            Evidence.Closed(inputs,inputKeys.ToArray());
            bool coverage=owner!=null&&IsCoverageAttempt(attempt);
            string version=owner==null?Contract:(coverage?"policy-qualified-runtime-protection-attempt-v2":"policy-qualified-runtime-protection-attempt-v1");
            Require(S(attempt,"contract_version")==version&&S(inputs,"contract_version")==version&&!(bool)attempt["execution_authorized"],"Protection contract version/authority mismatch");
            Require((inputs["attempt_ordinal"] is int||inputs["attempt_ordinal"] is long)&&Convert.ToInt64(inputs["attempt_ordinal"])>0,"Explicit positive attempt ordinal required");
            Require(S(inputs,"protection_source_identity")==SourceIdentity(),"Protection source/build identity mismatch");
            if(owner!=null)
            {
                if(coverage)Evidence.CoverageEnvelope(inputs["acceptance"]);
                else Require(S(O(inputs,"acceptance"),"contract_version")=="policy-qualified-downstream-envelope-v1","Old native attempt cannot reinterpret new coverage");
                owner.VerifyAttempt(attempt);
            }
            string identity=Evidence.Identity(owner==null?"runtime-protection-attempt:v1":(coverage?"policy-qualified-runtime-protection-attempt:v2":"policy-qualified-runtime-protection-attempt:v1"),inputs);
            Require(S(attempt,"attempt_identity")==identity,"Attempt identity mismatch");
            string space=Native.LocalPath(S(inputs,"namespace_root"),false);
            string suffix=identity.Substring(identity.LastIndexOf(':')+1);
            string root=Path.Combine(space,"attempts",suffix),payload=Path.Combine(root,"payload"),runtime=Path.Combine(payload,"runtime");
            var expected=new Dictionary<string,object> { {"attempt_root",root},{"payload_root",payload},{"private_runtime_root",runtime},
                {"private_app_root",Path.Combine(runtime,"app")},{"current_anchor",Path.Combine(payload,"anchors","current")},
                {"history_anchor",Path.Combine(payload,"anchors","history")},{"evidence_root",Path.Combine(root,"evidence")},
                {"claim_root",Path.Combine(space,"claims",suffix)} };
            Require(Evidence.Canonical(O(attempt,"paths"))==Evidence.Canonical(expected),"Attempt identity/path substitution");
            return expected;
        }
        static void EnvironmentContract(Dictionary<string,object> environment,bool ambient)
        {
            var names=new HashSet<string>(StringComparer.OrdinalIgnoreCase);
            foreach(var entry in environment)
            {
                Require(names.Add(entry.Key)&&entry.Key.IndexOfAny(new char[]{'\0','='})<0&&entry.Value is string&&((string)entry.Value).IndexOf('\0')<0,"Invalid sealed environment");
                Require(!Regex.IsMatch(entry.Key,"(?:API_KEY|TOKEN|SECRET|PASSWORD)$",RegexOptions.IgnoreCase),"Credential plaintext cannot enter public environment evidence");
                foreach(string key in ForbiddenEnvironment)Require(!String.Equals(key,entry.Key,StringComparison.OrdinalIgnoreCase),"Environment/search-path injection");
            }
            Require(environment.ContainsKey("TRADINGAGENTS_OUTPUT_LANGUAGE")&&S(environment,"TRADINGAGENTS_OUTPUT_LANGUAGE")=="English", "Sealed English environment mismatch");
            Require(environment.ContainsKey("PYTHON_DOTENV_DISABLED")&&S(environment,"PYTHON_DOTENV_DISABLED")=="1","Sealed dotenv contract mismatch");
            string system=Native.LocalPath(S(environment,"SystemRoot"),false);
            Require(String.Equals(system,Environment.GetFolderPath(Environment.SpecialFolder.Windows),StringComparison.OrdinalIgnoreCase),"Windows platform origin mismatch");
            Require(S(environment,"PATH")=="{private_runtime_root}\\base;{private_runtime_root}\\venv\\Scripts;"+system+"\\System32","Environment/search-path injection");
            if(environment.ContainsKey("TRADINGAGENTS_MAINLAND_CAPABILITY_ROUTING_MODE"))
                Require(S(environment,"TRADINGAGENTS_MAINLAND_CAPABILITY_ROUTING_MODE")!="qualified_v1","qualified_v1 authority is unavailable");
            if(ambient)foreach(string key in ForbiddenEnvironment)
                Require(String.IsNullOrEmpty(Environment.GetEnvironmentVariable(key)),"Ambient environment/search-path injection: "+key);
        }
        static string EnvironmentIdentity(Dictionary<string,object> environment,string cwd,object credentials)
        {
            Native.LocalPath(cwd,false);
            string previous=null;
            foreach(object item in (IEnumerable)credentials)
            {
                var row=Evidence.Obj(item);Evidence.Closed(row,"name","sha256");string name=S(row,"name");
                Require(Regex.IsMatch(name,"^[A-Z][A-Z0-9_]*(?:API_KEY|TOKEN|SECRET|PASSWORD)$")&&Regex.IsMatch(S(row,"sha256"),"^[0-9a-f]{64}$")
                    &&(previous==null||StringComparer.Ordinal.Compare(previous,name)<0),"Invalid/duplicate credential binding");previous=name;
            }
            return Evidence.Identity("runtime-protection-environment:v1",new Dictionary<string,object> { {"variables",environment},{"working_directory",cwd},{"credential_bindings",credentials} });
        }
        static void EnsureContainer(string path,bool immutable)
        {
            if(!Native.DirectoryExists(path))Native.CreateNewDirectory(path,immutable);
            using(var handle=Native.Open(path,Native.ReadAttributes,3)) { }
        }
        static Dictionary<string,object> NamespaceObjects(string root)
        {
            var result=new Dictionary<string,object>();
            foreach(string name in new string[]{"","attempts","claims"})
            {
                string path=name==""?root:Path.Combine(root,name);
                using(var handle=Native.Open(path,Native.ReadAttributes|Native.ReadControl,3))
                    result.Add(name,new Dictionary<string,object> {
                        {"object_identity",Native.ObjectId(handle)}, {"descriptor_identity",Native.Hash(Native.Descriptor(handle))} });
            }
            return result;
        }
        static Dictionary<string,object> Initialize(Dictionary<string,object> request)
        {
            Evidence.Closed(request,"contract_version","namespace_root","protection_source_identity");
            Require(S(request,"contract_version")=="runtime-protection-namespace-request-v1"&&S(request,"protection_source_identity")==SourceIdentity(),"Namespace source/contract mismatch");
            string root=Native.LocalPath(S(request,"namespace_root"),false);
            var anchors=Native.Anchor(root);
            try
            {
                // No existing namespace is initialized, repaired or re-registered.
                // Denying delete-child here protects permanent claims even from
                // the object's owner; object IDs also bind its external ancestry.
                using(var namespaceHandle=Native.CreateNewDirectoryHandle(root,true))
                using(var attemptsHandle=Native.CreateNewDirectoryHandle(Path.Combine(root,"attempts"),true))
                using(var claimsHandle=Native.CreateNewDirectoryHandle(Path.Combine(root,"claims"),true))
                {
                var receipt=Evidence.Seal("runtime-protection-namespace:v1",new Dictionary<string,object> {
                    {"contract_version","runtime-protection-namespace-v1"},{"namespace_root",root},
                    {"protection_source_identity",SourceIdentity()},{"objects",NamespaceObjects(root)} });
                Evidence.WriteNew(Path.Combine(root,"namespace.json"),receipt);return receipt;
                }
            }
            finally { foreach(var handle in anchors)handle.Dispose(); }
        }
        static PinnedJson HoldNamespace(Dictionary<string,object> inputs)
        {
            string root=S(inputs,"namespace_root");
            // The external attempt seal pins this document's semantic identity.
            // Its object identities prevent a deleted/recreated namespace from
            // restoring freshness to previously consumed attempt identities.
            var pinned=new PinnedJson(Path.Combine(root,"namespace.json"),null);
            try
            {
                var value=pinned.Value;Evidence.CheckIdentity(value,"runtime-protection-namespace:v1");
                Evidence.Closed(value,"contract_version","namespace_root","protection_source_identity","objects","identity");
                Require(S(value,"identity")==S(inputs,"namespace_identity")&&S(value,"namespace_root")==root&&S(value,"protection_source_identity")==SourceIdentity(),"Namespace provenance differs from attempt");
                Require(Evidence.Canonical(O(value,"objects"))==Evidence.Canonical(NamespaceObjects(root)),"Namespace object/security provenance mismatch");
                return pinned;
            }
            catch { pinned.Dispose();throw; }
        }
        static void ValidateComponent(Dictionary<string,object> component,string role,Dictionary<string,object> inputs)
        {
            Evidence.Closed(component,"contract_version","role","source_commit","source_tree","ancestry_identity","files","native_images","python_version","identity");
            Evidence.CheckIdentity(component,"runtime-protection-content:v1");
            Require(S(component,"contract_version")=="runtime-protection-content-v1"&&S(component,"role")==role,"Component contract mismatch");
            Require(S(component,"identity")==S(inputs,role+"_content_identity")&&S(component,"ancestry_identity")==S(inputs,role+"_ancestry_identity"),"Runtime/app manifest belongs to another attempt");
            Require(S(component,"source_commit")==S(inputs,"source_commit")&&S(component,"source_tree")==S(inputs,"source_tree"),"Qualified source mirror mismatch");
            var names=new HashSet<string>(StringComparer.OrdinalIgnoreCase);string previous=null;
            foreach(object value in (IEnumerable)component["files"])
            {
                var row=Evidence.Obj(value);Evidence.Closed(row,"relative_path","byte_length","sha256");string relative=S(row,"relative_path");
                Native.Child(@"C:\validation",relative);
                Require(names.Add(relative)&&(previous==null||StringComparer.Ordinal.Compare(previous,relative)<0),"Duplicate/noncanonical manifest members");previous=relative;
                string lower=relative.ToLowerInvariant();
                Require(!lower.EndsWith(".pyc")&&!lower.EndsWith(".pyo")&&!lower.EndsWith(".pth")&&!lower.EndsWith("._pth"),"Forbidden bytecode/search-path member");
                Require(Regex.IsMatch(S(row,"sha256"),"^[0-9a-f]{64}$")&&Convert.ToInt64(row["byte_length"])>=0,"Invalid manifest content identity");
                if(role=="runtime")Require((relative.StartsWith("base/",StringComparison.Ordinal)||relative.StartsWith("venv/",StringComparison.Ordinal))&&relative!="venv/pyvenv.cfg","Runtime/app ancestry mismatch");
            }
            var native=new HashSet<string>(StringComparer.OrdinalIgnoreCase);
            foreach(object value in (IEnumerable)component["native_images"])
                Require(native.Add(Evidence.Str(value))&&names.Contains(Evidence.Str(value)),"Unknown native dependency/image");
        }
        internal static Dictionary<string,object> Prepare(Dictionary<string,object> plan,IConditionalProtectionOwner owner=null)
        {
            var planKeys=new List<string>(new string[]{"contract_version","attempt","runtime_content","app_content","source_locations","deployment_configuration",
                "mapped_configuration","environment","working_directory","credential_bindings","python_version","execution_authorized","identity"});
            if(owner!=null)planKeys.Add("owner_binding");
            Evidence.Closed(plan,planKeys.ToArray());
            Evidence.CheckIdentity(plan,owner==null?"runtime-protection-materialization:v1":ConditionalKind("materialization",O(plan,"attempt"),true));
            Require(S(plan,"contract_version")== (owner==null?"runtime-protection-materialization-v1":ConditionalKind("materialization",O(plan,"attempt"),false))
                &&!(bool)plan["execution_authorized"],"Materialization authority mismatch");
            if(owner!=null)
            {
                Require(Evidence.Canonical(plan["owner_binding"])==Evidence.Canonical(owner.Binding),"Materialization has an unregistered owner binding");
                owner.VerifyMaterialization(plan);
            }
            var attempt=O(plan,"attempt");var inputs=O(attempt,"inputs");var paths=ValidateAttempt(attempt,owner);
            var runtime=O(plan,"runtime_content");var app=O(plan,"app_content");ValidateComponent(runtime,"runtime",inputs);ValidateComponent(app,"app",inputs);
            var nativeClosure=new NativePayloadClosure(runtime,app);
            Require(S(runtime,"python_version")==S(plan,"python_version")&&Regex.IsMatch(S(plan,"python_version"),"^3\\.[0-9]+\\.[0-9]+$"),"pyvenv.cfg version ancestry mismatch");
            var config=O(plan,"deployment_configuration");var mapped=O(plan,"mapped_configuration");var environment=O(plan,"environment");
            Require(Evidence.Identity("runtime-protection-configuration:v1",config)==S(inputs,"deployment_configuration_identity"),"Deployment configuration identity mismatch");
            Require(EnvironmentIdentity(environment,S(plan,"working_directory"),plan["credential_bindings"])==S(inputs,"environment_identity"),"Environment identity mismatch");EnvironmentContract(environment,false);
            var expected=new Dictionary<string,object>(config);string appRoot=S(paths,"private_app_root");
            Require(config.ContainsKey("project_dir")&&config.ContainsKey("crypto_identity_registry_path")&&config.ContainsKey("crypto_identity_registry_checksum_path"),"Deployment path authority incomplete");
            expected["project_dir"]=Path.Combine(appRoot,"tradingagents");expected["crypto_identity_registry_path"]=Path.Combine(appRoot,"tradingagents","config","crypto_identity_registry.json");
            expected["crypto_identity_registry_checksum_path"]=Path.Combine(appRoot,"tradingagents","config","crypto_identity_registry.sha256");
            Require(Evidence.Canonical(expected)==Evidence.Canonical(mapped),"Unauthorized deployment configuration difference");
            if(config.ContainsKey("mainland_capability_routing_mode"))Require(S(config,"mainland_capability_routing_mode")!="qualified_v1","qualified_v1 authority is unavailable");
            string space=S(inputs,"namespace_root"),root=S(paths,"attempt_root"),claim=S(paths,"claim_root");
            foreach(var location in O(plan,"source_locations"))
            {
                string source=Native.LocalPath(Evidence.Str(location.Value),false);
                Require(!source.StartsWith(space+"\\",StringComparison.OrdinalIgnoreCase),"Previous attempt cannot supply materialization bytes");
                using(var pins=new AncestorPins(source))
                using(var leaf=Native.Open(source,Native.ReadAttributes,1))
                    Require((Native.Information(leaf).Attributes&0x10)==0,"Materialization source must be a regular file");
            }
            using(var registered=HoldNamespace(inputs))
            {
                Require(!Native.DirectoryExists(root)&&!Native.FileExists(root),"Attempt root exists without fresh provenance");
                Require(!Native.DirectoryExists(claim)&&!Native.FileExists(claim),"Attempt provenance already exists; consumed or incomplete attempt");
                using(var claimHandle=Native.CreateNewDirectoryHandle(claim,true)) // Permanent allocation precedes every attempt object.
                using(var rootHandle=Native.CreateNewDirectoryHandle(root,true))
                using(var evidenceHandle=Native.CreateNewDirectoryHandle(S(paths,"evidence_root"),true))
                {
                var allocation=Evidence.Seal("runtime-protection-allocation:v1",new Dictionary<string,object> {
                    {"contract_version","runtime-protection-allocation-v1"},{"attempt_identity",S(attempt,"attempt_identity")},
                    {"materialization_identity",S(plan,"identity")},{"paths",paths},{"state","PREPARING"} });
                Evidence.WriteNew(Path.Combine(S(paths,"evidence_root"),"allocation.json"),allocation);
                using(var tree=FreshProtectionTree.CreateReserved(S(paths,"payload_root"),claim))
                {
                    var locations=O(plan,"source_locations");var expectedLocations=new HashSet<string>(StringComparer.Ordinal);
                    foreach(var component in new Dictionary<string,object>[] {runtime,app})
                    {
                        string role=S(component,"role");var natives=new HashSet<string>(StringComparer.Ordinal);
                        foreach(object image in (IEnumerable)component["native_images"])natives.Add(Evidence.Str(image));
                        foreach(object value in (IEnumerable)component["files"])
                        {
                            var row=Evidence.Obj(value);string relative=S(row,"relative_path"),key=role+"/"+relative;expectedLocations.Add(key);
                            string source=Native.LocalPath(S(locations,key),false);
                            Require(!source.StartsWith(space+"\\",StringComparison.OrdinalIgnoreCase),"Previous attempt cannot supply materialization bytes");
                            var sourceAnchors=Native.Anchor(source);
                            try
                            {
                                using(var leaf=Native.Open(source,Native.ReadData|Native.ReadAttributes,1))
                                using(var stream=new FileStream(leaf,FileAccess.Read))
                                {
                                    Native.Information(stream.SafeFileHandle);
                                    Require(stream.Length==Convert.ToInt64(row["byte_length"])&&stream.Length<=Int32.MaxValue,"Component byte length drift");
                                    byte[] bytes=new byte[stream.Length];int offset=0;
                                    while(offset<bytes.Length) { int count=stream.Read(bytes,offset,bytes.Length-offset);if(count==0)throw new EndOfStreamException();offset+=count; }
                                    Require(Native.Hash(bytes)==S(row,"sha256"),"Component/source mirror byte drift");
                                    bool mz=bytes.Length>=2&&bytes[0]==77&&bytes[1]==90;
                                    Require(mz==natives.Contains(relative),"Unknown native executable dependency/image");
                                    if(mz)nativeClosure.Verify(bytes);
                                    tree.WriteNewFile(role=="runtime"?"runtime/"+relative:"runtime/app/"+relative,bytes);
                                }
                            }
                            finally { foreach(var handle in sourceAnchors)handle.Dispose(); }
                        }
                    }
                    Require(expectedLocations.SetEquals(locations.Keys),"Unexpected materialization source location");
                    tree.CreateDirectory("anchors/current");tree.CreateDirectory("anchors/history");tree.CreateDirectory("runtime/app");
                    var historyBinding=new Dictionary<string,object> {
                        {"contract_version",owner==null?"runtime-protection-history-reference-v1":ConditionalKind("history-reference",O(plan,"attempt"),false)},{"attempt_identity",S(attempt,"attempt_identity")},
                        {"preparation_lineage_identity",S(inputs,"preparation_lineage_identity")},
                        {"production_preparation_identity",S(inputs,"production_preparation_identity")},
                        {"qualified_source_identity",S(inputs,"qualified_source_identity")},{"historical_objects_mutated",false} };
                    if(owner!=null) { historyBinding.Add("acceptance",inputs["acceptance"]);historyBinding.Add("owner_binding",owner.Binding);historyBinding.Add("execution_authorized",false); }
                    tree.WriteNewFile("anchors/history/provenance.json",Encoding.UTF8.GetBytes(Evidence.Canonical(historyBinding)));
                    string runtimeRoot=S(paths,"private_runtime_root");
                    tree.WriteNewFile("runtime/venv/pyvenv.cfg",Encoding.UTF8.GetBytes("home = "+Path.Combine(runtimeRoot,"base")+"\ninclude-system-site-packages = false\nversion = "+S(plan,"python_version")+"\nexecutable = "+Path.Combine(runtimeRoot,"base","python.exe")+"\n"));
                    tree.WriteNewFile("runtime/venv/Lib/site-packages/zz_ticket07_private_app.pth",Encoding.UTF8.GetBytes(appRoot+"\n"));
                    tree.WriteNewFile("runtime/configuration.json",Encoding.UTF8.GetBytes(Evidence.Canonical(mapped)));
                    var baselines=new List<object>();foreach(var entry in tree.CaptureBaseline())baselines.Add(Evidence.Baseline(entry));
                    var preparedBody=new Dictionary<string,object> {
                        {"contract_version",owner==null?"runtime-protection-prepared-v1":ConditionalKind("prepared",O(plan,"attempt"),false)},{"attempt",attempt},{"materialization_identity",S(plan,"identity")},
                        {"runtime_manifest_identity",S(runtime,"identity")},{"app_manifest_identity",S(app,"identity")},
                        {"baseline",baselines},{"baseline_identity",Evidence.Identity("runtime-protection-baseline:v1",baselines)},
                        {"environment",environment},{"working_directory",S(plan,"working_directory")},{"mapped_configuration",mapped},{"protection_contract_version","protected-runtime-tree-v3"},
                        {"credential_bindings",plan["credential_bindings"]},
                        {"state","PREPARED"},{"execution_authorized",false} };
                    if(owner!=null)preparedBody.Add("owner_binding",owner.Binding);
                    var prepared=Evidence.Seal(owner==null?"runtime-protection-prepared:v1":ConditionalKind("prepared",O(plan,"attempt"),true),preparedBody);
                    Evidence.WriteNew(Path.Combine(S(paths,"evidence_root"),"prepared.json"),prepared);
                    Native.CreateNewDirectory(Path.Combine(claim,"prepared-"+S(prepared,"identity").Substring(S(prepared,"identity").LastIndexOf(':')+1)),true);
                    return prepared;
                }
                }
            }
        }
        static string Quote(string value)
        {
            var result=new StringBuilder("\"");int slashes=0;
            foreach(char character in value)
            {
                if(character=='\\') { slashes++;continue; }
                if(character=='"') { result.Append('\\',slashes*2+1).Append('"');slashes=0;continue; }
                result.Append('\\',slashes).Append(character);slashes=0;
            }
            return result.Append('\\',slashes*2).Append('"').ToString();
        }
        static void ValidateHandoff(Dictionary<string,object> prepared,Dictionary<string,object> packet,IConditionalProtectionOwner owner=null)
        {
            var gate=O(packet,"handoff");var inputs=O(O(prepared,"attempt"),"inputs");var config=O(prepared,"mapped_configuration");
            var keys=new List<string>(new string[]{"contract_version","preparation_lineage_identity","plan_version","plan_signature","cninfo_profile_identity",
                "cninfo_source_policy_identity","cninfo_operator_minimum_interval_seconds","checkpoint_identity","history_identity",
                "calendar_revision_id","valid_before_exclusive","symbol","analysis_session","selected_analysts","output_language",
                "legacy_authoritative","qualified_shadow_authoritative","execution_authorized"});
            if(owner!=null) { keys.Add("acceptance");keys.Add("owner_binding"); }
            Evidence.Closed(gate,keys.ToArray());
            Require(S(gate,"contract_version")== (owner==null?"runtime-protection-handoff-v1":ConditionalKind("handoff",O(prepared,"attempt"),false))&&S(gate,"plan_version")=="1.5"
                &&S(gate,"preparation_lineage_identity")==S(inputs,"preparation_lineage_identity")
                &&S(gate,"analysis_session")==S(inputs,"completed_session"),"Handoff preparation/session/Plan mismatch");
            object pacing=gate["cninfo_operator_minimum_interval_seconds"];
            Require((pacing is int||pacing is long||pacing is decimal||pacing is double)
                &&Convert.ToDouble(pacing)>0&&!Double.IsInfinity(Convert.ToDouble(pacing))
                &&Convert.ToDouble(pacing)==Convert.ToDouble(config["cninfo_operator_minimum_interval_seconds"]),"Operator-selected CNINFO pacing mismatch");
            Require(S(gate,"output_language")=="English"&&S(config,"output_language")=="English"
                &&S(config,"mainland_capability_routing_mode")=="qualified_v1_shadow"
                &&(bool)gate["legacy_authoritative"]&&!(bool)gate["qualified_shadow_authoritative"]&&!(bool)gate["execution_authorized"],"Handoff authority/language mismatch");
            Require(Evidence.Canonical(gate["selected_analysts"])=="[\"fundamentals\"]","Step-4 analyst scope differs");
            if(owner!=null)
            {
                Require(Evidence.Canonical(gate["acceptance"])==Evidence.Canonical(inputs["acceptance"])
                    &&Evidence.Canonical(gate["owner_binding"])==Evidence.Canonical(owner.Binding),"Handoff stripped or substituted the conditional binding");
                Require(S(packet,"operation")=="guard_only","Conditional qualification is not separate Ticket-07/live authorization");
            }
            RequireFreshHandoff(gate);
            object expected=null;
            if(S(packet,"operation")=="guard_then_live")expected=new string[]{"-I","-B","-m","cli.main","analyze","--qualified-routing-validation",
                "--qualified-routing-calendar-revision-id",S(gate,"calendar_revision_id"),"--checkpoint"};
            Require(Evidence.Canonical(packet["live_child_arguments"])==Evidence.Canonical(expected),"Live command differs from qualified CLI contract");
        }
        static void RequireFreshHandoff(Dictionary<string,object> gate)
        {
            string text=S(gate,"valid_before_exclusive");DateTimeOffset expiry;
            Require((text.EndsWith("Z")||text.EndsWith("+00:00"))&&DateTimeOffset.TryParse(text,CultureInfo.InvariantCulture,DateTimeStyles.None,out expiry),"Explicit UTC handoff expiry required");
            expiry=DateTimeOffset.Parse(text,CultureInfo.InvariantCulture);
            Require(DateTimeOffset.UtcNow<expiry,"Protected handoff expired");
        }
        [StructLayout(LayoutKind.Sequential)] struct GuardPipeAttributes { internal int Length;internal IntPtr Descriptor;internal int Inherit; }
        [DllImport("kernel32.dll",CharSet=CharSet.Unicode,SetLastError=true)]
        static extern SafePipeHandle CreateNamedPipeW(string name,uint mode,uint pipeMode,uint instances,uint outputSize,uint inputSize,uint timeout,ref GuardPipeAttributes security);
        internal static NamedPipeServerStream OwnerPipe(string attemptIdentity,bool restrictedChild=false)
        {
            string name="TradingAgentsProtection-"+attemptIdentity.Substring(attemptIdentity.LastIndexOf(':')+1);
            if(restrictedChild)
            {
                // Only this new pipe receives a medium label. The high owner
                // process, protected files and legacy pipe retain their labels.
                byte[] descriptor=Native.FromSddl("D:P(A;;GA;;;SY)(A;;GA;;;BA)(A;;0x0012019F;;;"
                    +WindowsIdentity.GetCurrent().User.Value+")S:(ML;;NW;;;ME)");
                var pin=GCHandle.Alloc(descriptor,GCHandleType.Pinned);
                try
                {
                    var attributes=new GuardPipeAttributes {Length=Marshal.SizeOf(typeof(GuardPipeAttributes)),Descriptor=pin.AddrOfPinnedObject()};
                    var handle=CreateNamedPipeW(@"\\.\pipe\"+name,0x40080003,8,1,4096,4096,10000,ref attributes);
                    if(handle.IsInvalid) { int error=Marshal.GetLastWin32Error();handle.Dispose();throw new Win32Exception(error); }
                    return new NamedPipeServerStream(PipeDirection.InOut,true,false,handle);
                }
                finally { pin.Free(); }
            }
            var security=new PipeSecurity();
            security.SetAccessRuleProtection(true,false);
            security.AddAccessRule(new PipeAccessRule(WindowsIdentity.GetCurrent().User,PipeAccessRights.FullControl,AccessControlType.Allow));
            return new NamedPipeServerStream(name,PipeDirection.InOut,1,PipeTransmissionMode.Byte,PipeOptions.Asynchronous,4096,4096,security);
        }
        static Dictionary<string,object> ChildContext(Dictionary<string,object> prepared,Dictionary<string,object> packet,string phase)
        {
            var inputs=O(O(prepared,"attempt"),"inputs");
            return new Dictionary<string,object> {
                {"phase",phase},{"attempt_identity",S(packet,"attempt_identity")},{"packet_identity",S(packet,"identity")},
                {"prepared_identity",S(prepared,"identity")},{"owner_pid",Process.GetCurrentProcess().Id},
                {"owner_creation_time",Process.GetCurrentProcess().StartTime.ToUniversalTime().ToFileTimeUtc()},
                {"runtime_manifest_identity",prepared["runtime_manifest_identity"]},{"app_manifest_identity",prepared["app_manifest_identity"]},
                {"environment_identity",inputs["environment_identity"]},{"runtime_config_identity",Evidence.Identity("runtime-protection-deployed-configuration:v1",prepared["mapped_configuration"])},
                {"prospective_thread",S(inputs,"prospective_thread")},{"prospective_run",S(inputs,"prospective_run")},{"handoff",packet["handoff"]} };
        }
        static Dictionary<string,object> ReceivePhase(NamedPipeServerStream pipe,Process child,ProtectedChildren children,ProtectedRuntimeTreeV3 protection,
            Dictionary<string,object> prepared,Dictionary<string,object> packet,string phase,IConditionalProtectionOwner owner=null,ChildDeadline deadline=null)
        {
            var connected=Task.Factory.FromAsync(pipe.BeginWaitForConnection,pipe.EndWaitForConnection,null);
            var timer=Stopwatch.StartNew();
            while(!connected.Wait(100))
            {
                if(deadline!=null)deadline.Check();
                Require(!child.HasExited,"Protected child exited before owner-bound phase");
                Require(timer.ElapsedMilliseconds<600000,"Protected child phase timed out");
                RequireFreshHandoff(O(packet,"handoff"));
            }
            uint processId;Require(Native.GetNamedPipeClientProcessId(pipe.SafePipeHandle,out processId),"Phase client process unavailable");
            using(var actualChild=children.ValidateClient(processId,child,Path.Combine(S(O(O(prepared,"attempt"),"paths"),"private_runtime_root"),"base","python.exe")))
            using(var bytes=new MemoryStream())
            {
                var one=new byte[1];
                while(true)
                {
                    var read=pipe.ReadAsync(one,0,1);
                    Require(read.Wait(deadline==null?30000:Math.Min(30000,deadline.Remaining))&&read.Result==1,"Protected phase message incomplete");
                    if(deadline!=null)deadline.Check();
                    if(one[0]==10)break;
                    bytes.WriteByte(one[0]);Require(bytes.Length<=65536&&timer.ElapsedMilliseconds<600000,"Protected phase message exceeds bound");
                }
                string text=new UTF8Encoding(false,true).GetString(bytes.ToArray());
                var message=Evidence.Obj(new JavaScriptSerializer().DeserializeObject(text));
                Require(Evidence.Canonical(message)==text,"Protected phase message must be canonical");
                Require(S(message,"phase")==phase&&S(message,"attempt_identity")==S(packet,"attempt_identity")&&S(message,"packet_identity")==S(packet,"identity"),"Guard/Step-4 belongs to another attempt or packet");
                var gate=O(packet,"handoff");RequireFreshHandoff(gate);protection.VerifyProtected();
                if(owner!=null)owner.VerifyAttempt(O(prepared,"attempt"));
                if(deadline!=null)deadline.Check();
                if(phase=="guard")
                {
                    var messageKeys=new List<string>(new string[]{"contract_version","phase","attempt_identity","prepared_identity","packet_identity","source_guard_identity",
                        "checkpoint_identity","history_identity","prospective_thread","prospective_run","status","live_io",
                        "runtime_manifest_identity","app_manifest_identity","environment_identity","runtime_config_identity",
                        "plan_signature","cninfo_profile_identity","cninfo_source_policy_identity","cninfo_operator_minimum_interval_seconds",
                        "legacy_authoritative","qualified_shadow_authoritative",
                        "checkpoint_original_exact_equal","retained_provenance_changed","run_or_thread_allocated","execution_authorized","identity"});
                    if(owner!=null) { messageKeys.Add("acceptance");messageKeys.Add("owner_binding"); }
                    Evidence.Closed(message,messageKeys.ToArray());
                    Evidence.CheckIdentity(message,owner==null?"runtime-protection-guard:v1":ConditionalKind("guard",O(prepared,"attempt"),true));var inputs=O(O(prepared,"attempt"),"inputs");
                    Require(S(message,"contract_version")== (owner==null?"runtime-protection-guard-v1":ConditionalKind("guard",O(prepared,"attempt"),false))&&S(message,"status")=="PASS"
                        &&S(message,"prepared_identity")==S(prepared,"identity")&&S(message,"checkpoint_identity")==S(gate,"checkpoint_identity")
                        &&S(message,"history_identity")==S(gate,"history_identity")&&S(message,"prospective_thread")==S(inputs,"prospective_thread")
                        &&S(message,"prospective_run")==S(inputs,"prospective_run"),"Guard history/lineage evidence mismatch");
                    if(owner!=null)Require(Evidence.Canonical(message["acceptance"])==Evidence.Canonical(inputs["acceptance"])
                        &&Evidence.Canonical(message["owner_binding"])==Evidence.Canonical(owner.Binding),"Protected guard lost the complete conditional binding");
                    Require((bool)message["checkpoint_original_exact_equal"]&&!(bool)message["retained_provenance_changed"]
                        &&!(bool)message["run_or_thread_allocated"]&&!(bool)message["execution_authorized"]
                        &&Evidence.Canonical(message["live_io"])=="{\"CNINFO\":0,\"model\":0,\"network\":0,\"provider\":0}","Guard exceeded zero-I/O/history authority");
                    var context=ChildContext(prepared,packet,phase);
                    foreach(string key in new string[]{"runtime_manifest_identity","app_manifest_identity","environment_identity","runtime_config_identity"})
                        Require(S(message,key)==S(context,key),"Guard runtime/config/environment binding differs");
                    foreach(string key in new string[]{"plan_signature","cninfo_profile_identity","cninfo_source_policy_identity",
                        "cninfo_operator_minimum_interval_seconds","legacy_authoritative","qualified_shadow_authoritative"})
                        Require(Evidence.Canonical(message[key])==Evidence.Canonical(gate[key]),"Guard qualified plan/profile/policy differs");
                    VerifyLoadedImages(actualChild,prepared);
                    if(deadline!=null)deadline.Check();
                    Evidence.WriteNew(Path.Combine(S(O(O(prepared,"attempt"),"paths"),"evidence_root"),"guard.json"),message);
                }
                else
                {
                    Evidence.Closed(message,"phase","attempt_identity","packet_identity","questionnaire","literal_confirmation","runtime_config_identity");
                    Require(S(message,"literal_confirmation")=="YES","Literal human Step-4 confirmation required");
                    Require(S(message,"runtime_config_identity")==Evidence.Identity("runtime-protection-deployed-configuration:v1",prepared["mapped_configuration"]),"Actual Step-4 runtime configuration differs");
                    var expected=new Dictionary<string,object>();foreach(string key in new string[]{"symbol","analysis_session","selected_analysts","output_language"})expected.Add(key,gate[key]);
                    Require(Evidence.Canonical(message["questionnaire"])==Evidence.Canonical(expected),"Step-4 questionnaire differs from sealed packet");
                    var receipt=Evidence.Seal("runtime-protection-step4:v1",new Dictionary<string,object> {
                        {"contract_version","runtime-protection-step4-v1"},{"attempt_identity",S(packet,"attempt_identity")},
                        {"packet_identity",S(packet,"identity")},{"owner_pid",Process.GetCurrentProcess().Id},{"child_pid",actualChild.Id},
                        {"questionnaire",expected},{"literal_confirmation","YES"},{"protected_interval_active",true},
                        {"runtime_config_identity",S(message,"runtime_config_identity")},
                        {"issued_at",DateTimeOffset.UtcNow.ToString("o")},{"reusable",false} });
                    Evidence.WriteNew(Path.Combine(S(O(O(prepared,"attempt"),"paths"),"evidence_root"),"step4.json"),receipt);
                }
                var responseBody=new Dictionary<string,object> {
                    {"phase",phase},{"attempt_identity",S(packet,"attempt_identity")},{"packet_identity",S(packet,"identity")},{"accepted",true} };
                if(owner!=null)
                {
                    owner.VerifyAttempt(O(prepared,"attempt"));
                    if(deadline!=null)deadline.Check();
                    responseBody.Add("acceptance",O(O(prepared,"attempt"),"inputs")["acceptance"]);
                    responseBody.Add("owner_binding",owner.Binding);responseBody.Add("execution_authorized",false);
                }
                byte[] response=Encoding.UTF8.GetBytes(Evidence.Canonical(responseBody)+"\n");
                if(deadline==null) { pipe.Write(response,0,response.Length);pipe.Flush(); }
                else { Require(pipe.WriteAsync(response,0,response.Length).Wait(deadline.Remaining),"Conditional guard release exceeded child deadline");deadline.Check(); }
                return message;
            }
        }
        static void VerifyLoadedImages(Process child,Dictionary<string,object> prepared)
        {
            string payload=S(O(O(prepared,"attempt"),"paths"),"payload_root"),windows=Environment.GetFolderPath(Environment.SpecialFolder.Windows);
            var members=new Dictionary<string,BaselineEntry>(StringComparer.OrdinalIgnoreCase);
            foreach(var row in Evidence.Baselines(prepared["baseline"]))if(!row.Directory)members.Add(row.RelativePath,row);
            foreach(ProcessModule module in child.Modules)
            {
                string path=Native.LocalPath(module.FileName,false);var ancestors=Native.Anchor(path);
                try
                {
                    bool privateImage=path.StartsWith(payload+"\\",StringComparison.OrdinalIgnoreCase);
                    using(var handle=privateImage?Native.Open(path,Native.ReadData|Native.ReadControl|Native.ReadAttributes,1):Native.OpenWindowsPlatformImage(path))
                    {
                        if(privateImage)
                        {
                            BaselineEntry entry;Require(members.TryGetValue(Native.Relative(payload,path),out entry)
                                &&Native.ObjectId(handle)==entry.ObjectIdentity&&Native.HashFile(handle)==entry.ContentSha256,"Unknown loaded private native image");
                        }
                    }
                }
                finally { foreach(var handle in ancestors)handle.Dispose(); }
            }
        }
        static Dictionary<string,object> Inspect(Dictionary<string,object> prepared)
        {
            Evidence.CheckIdentity(prepared,"runtime-protection-prepared:v1");
            var attempt=O(prepared,"attempt");var paths=ValidateAttempt(attempt);var inputs=O(attempt,"inputs");
            Require(S(prepared,"contract_version")=="runtime-protection-prepared-v1"&&S(prepared,"state")=="PREPARED"
                &&S(prepared,"protection_contract_version")=="protected-runtime-tree-v3","Prepared contract mismatch");
            Require(S(prepared,"baseline_identity")==Evidence.Identity("runtime-protection-baseline:v1",prepared["baseline"]),"Security baseline identity mismatch");
            using(var registered=HoldNamespace(inputs))
            {
                string claim=S(paths,"claim_root"),digest=S(prepared,"identity").Substring(S(prepared,"identity").LastIndexOf(':')+1);
                Require(Native.DirectoryExists(Path.Combine(claim,"prepared-"+digest)),"Prepared claim provenance missing");
                bool consumed=Native.DirectoryExists(Path.Combine(claim,"consumed")),matches=true;
                var expected=new Dictionary<string,BaselineEntry>(StringComparer.Ordinal);
                foreach(var entry in Evidence.Baselines(prepared["baseline"]))expected.Add(entry.RelativePath,entry);
                // Read-only, including stranded trees. Never call OpenPrepared,
                // capture a new baseline, request WRITE_DAC, or restore here.
                string root=S(paths,"payload_root");var actual=FreshProtectionTree.Walk(root);
                if(actual.Count!=expected.Count)matches=false;
                foreach(string path in actual)
                {
                    using(var handle=Native.Open(path,Native.ReadData|Native.ReadAttributes|Native.ReadControl,1))
                    {
                        BaselineEntry entry;
                        if(!expected.TryGetValue(Native.Relative(root,path),out entry)||!FreshProtectionTree.Same(entry,FreshProtectionTree.Capture(root,path,handle)))matches=false;
                    }
                }
                string state=consumed?"INCOMPLETE_RETAINED":"PREPARED";
                Dictionary<string,object> completion=null;string completionPath=Path.Combine(S(paths,"evidence_root"),"completion.json");
                if(Native.FileExists(completionPath))
                {
                    using(var pinned=new PinnedJson(completionPath,null))completion=pinned.Value;
                    Evidence.CheckIdentity(completion,"runtime-protection-completion:v1");
                    Require(consumed&&S(completion,"attempt_identity")==S(attempt,"attempt_identity")&&S(completion,"prepared_identity")==S(prepared,"identity")
                        &&!(bool)completion["reusable"],"Completion provenance mismatch");
                    state=S(completion,"state");
                    Require((state=="COMPLETED_RESTORED")==((bool)completion["exact_baseline_restored"])
                        &&(state=="COMPLETED_RESTORED"||state=="INCOMPLETE_RETAINED"),"Completion state mismatch");
                }
                return Evidence.Seal("runtime-protection-observation:v1",new Dictionary<string,object> {
                    {"contract_version","runtime-protection-observation-v1"},{"attempt_identity",S(attempt,"attempt_identity")},
                    {"prepared_identity",S(prepared,"identity")},{"consumed",consumed},{"baseline_matches",matches},
                    {"state",state},{"completion",completion} });
            }
        }
        internal static Dictionary<string,object> Run(Dictionary<string,object> prepared,Dictionary<string,object> packet,IConditionalProtectionOwner owner=null)
        {
            Evidence.CheckIdentity(prepared,owner==null?"runtime-protection-prepared:v1":ConditionalKind("prepared",O(prepared,"attempt"),true));
            Evidence.CheckIdentity(packet,owner==null?"runtime-protection-packet:v1":ConditionalKind("control",O(prepared,"attempt"),true));
            Require(S(prepared,"contract_version")== (owner==null?"runtime-protection-prepared-v1":ConditionalKind("prepared",O(prepared,"attempt"),false))&&S(prepared,"state")=="PREPARED", "Prepared contract/state mismatch");
            var attempt=O(prepared,"attempt");var paths=ValidateAttempt(attempt,owner);var inputs=O(attempt,"inputs");
            Require(S(packet,"contract_version")== (owner==null?"runtime-protection-packet-v1":ConditionalKind("control",O(prepared,"attempt"),false))&&S(packet,"prepared_identity")==S(prepared,"identity"),"Packet/preparation lineage mismatch");
            Require(S(packet,"attempt_identity")==S(attempt,"attempt_identity")&&Evidence.Canonical(O(packet,"paths"))==Evidence.Canonical(paths),"Packet attempt/root/anchor substitution");
            foreach(string key in new string[]{"materialization_identity","runtime_manifest_identity","app_manifest_identity","baseline_identity","protection_contract_version"})
                Require(S(packet,key)==S(prepared,key),"Packet runtime/app/baseline/protection version mismatch");
            Require(S(prepared,"protection_contract_version")=="protected-runtime-tree-v3", "Protection version differs");
            Require(S(prepared,"baseline_identity")==Evidence.Identity("runtime-protection-baseline:v1",prepared["baseline"]),"Security baseline identity mismatch");
            Require(S(prepared,"runtime_manifest_identity")==S(inputs,"runtime_content_identity")&&S(prepared,"app_manifest_identity")==S(inputs,"app_content_identity"),"Manifest belongs to another attempt");
            var environment=O(prepared,"environment");EnvironmentContract(environment,true);
            var inheritedDll=new StringBuilder(32768);
            Require(Native.GetDllDirectoryW((uint)inheritedDll.Capacity,inheritedDll)==0,"Uncontrolled inherited native DLL directory");
            Require(EnvironmentIdentity(environment,S(prepared,"working_directory"),prepared["credential_bindings"])==S(inputs,"environment_identity"),"Environment seal differs");
            string operation=S(packet,"operation");bool guarded=operation=="guard_only"||operation=="guard_then_live";
            Require((operation=="inert"||guarded)&&!(bool)packet["execution_authorized"],"Execution requires a separately bound handoff authority");
            var packetKeys=new List<string>(new string[]{"contract_version","operation","prepared_identity","attempt_identity","paths",
                "materialization_identity","runtime_manifest_identity","app_manifest_identity","baseline_identity","protection_contract_version",
                "child_arguments","execution_authorized","identity"});
            if(owner!=null)
            {
                packetKeys.Add("owner_binding");packetKeys.Add("acceptance");
                Require(Evidence.Canonical(prepared["owner_binding"])==Evidence.Canonical(owner.Binding)
                    &&Evidence.Canonical(packet["owner_binding"])==Evidence.Canonical(owner.Binding)
                    &&Evidence.Canonical(packet["acceptance"])==Evidence.Canonical(inputs["acceptance"]),"Native control stripped or replaced the conditional binding");
                Require(operation=="inert"||operation=="guard_only","Conditional qualification supplies no live authorization");
                owner.RequireInertLaunch(prepared,packet);
            }
            if(guarded) { packetKeys.Add("handoff");packetKeys.Add("live_child_arguments");ValidateHandoff(prepared,packet,owner); }
            Evidence.Closed(packet,packetKeys.ToArray());
            if(operation=="guard_then_live")
            {
                ValidateLiveApplication(prepared);
                Require(!Native.DirectoryExists(Path.Combine(S(paths,"claim_root"),"consumed")),"Attempt already consumed");
                Console.Error.WriteLine("Type the exact packet identity covered by separate fresh pre-launch authorization:");
                Require(Console.ReadLine()==S(packet,"identity"),"Fresh human pre-launch packet identity mismatch");
                Console.Error.WriteLine("Confirm separate fresh pre-launch authorization for this packet (type YES):");
                Require(Console.ReadLine()=="YES","Separate fresh pre-launch authorization confirmation required");
            }
            var arguments=new List<string>();foreach(object value in (IEnumerable)packet["child_arguments"])arguments.Add(Evidence.Str(value));
            Require(arguments.Count>=3&&arguments[0]=="-I"&&arguments[1]=="-B","Sealed interpreter startup flags differ");
            Require(Native.LocalPath(arguments[2],false).StartsWith(S(paths,"private_app_root")+"\\",StringComparison.OrdinalIgnoreCase),"Entrypoint outside private app");
            string executable=Path.Combine(S(paths,"private_runtime_root"),"venv","Scripts","python.exe");
            var baseline=Evidence.Baselines(prepared["baseline"]);string digest=S(prepared,"identity").Substring(S(prepared,"identity").LastIndexOf(':')+1);
            using(var registered=HoldNamespace(inputs))
            using(var tree=FreshProtectionTree.OpenPrepared(S(paths,"payload_root"),S(paths,"claim_root"),baseline,digest))
            using(var cwdAncestry=new AncestorPins(S(prepared,"working_directory")))
            using(var cwd=Native.Open(S(prepared,"working_directory"),Native.ReadAttributes,3))
            using(var children=new ProtectedChildren())
            {
                ProtectedRuntimeTreeV3 protection=null;Process child=null;NamedPipeServerStream pipe=null;ChildDeadline deadline=null;
                int starts=0;var failures=new List<Exception>();bool restored=false,dllChanged=false;
                try
                {
                    protection=tree.Protect(baseline);
                    var launchBody=new Dictionary<string,object> {
                        {"contract_version",owner==null?"runtime-protection-launch-v1":ConditionalKind("launch",O(prepared,"attempt"),false)},{"attempt_identity",S(attempt,"attempt_identity")},
                        {"packet_identity",S(packet,"identity")},{"state","LAUNCHING"} };
                    if(owner!=null) { launchBody.Add("acceptance",inputs["acceptance"]);launchBody.Add("owner_binding",owner.Binding);launchBody.Add("execution_authorized",false); }
                    var launch=Evidence.Seal(owner==null?"runtime-protection-launch:v1":ConditionalKind("launch",O(prepared,"attempt"),true),launchBody);
                    Evidence.WriteNew(Path.Combine(S(paths,"evidence_root"),"launching.json"),launch);
                    Require(Native.SetDllDirectoryW(Path.Combine(S(paths,"private_runtime_root"),"base")),"Private native DLL search directory failed");dllChanged=true;
                    int phases=operation=="guard_then_live"?2:1;
                    for(int index=0;index<phases;index++)
                    {
                        string phase=index==0?(guarded?"guard":"inert"):"step4";
                        if(index>0)
                        {
                            child.Dispose();child=null;
                            arguments.Clear();foreach(object argument in (IEnumerable)packet["live_child_arguments"])arguments.Add(Evidence.Str(argument));
                        }
                        if(guarded) { RequireFreshHandoff(O(packet,"handoff"));pipe=OwnerPipe(S(packet,"attempt_identity"),owner!=null); }
                        var info=new ProcessStartInfo { FileName=executable,WorkingDirectory=S(prepared,"working_directory"),UseShellExecute=false,CreateNoWindow=phase!="step4" };
                        var quoted=new List<string>();foreach(string argument in arguments)quoted.Add(Quote(argument));info.Arguments=String.Join(" ",quoted);
                        info.EnvironmentVariables.Clear();foreach(var entry in environment)info.EnvironmentVariables[entry.Key]=Evidence.Str(entry.Value);
                        info.EnvironmentVariables["PATH"]=S(environment,"PATH").Replace("{private_runtime_root}",S(paths,"private_runtime_root"));
                        foreach(object value in (IEnumerable)prepared["credential_bindings"])
                        {
                            var credential=Evidence.Obj(value);string name=S(credential,"name"),credentialValue="ticket07-inert-presence-only-no-network";
                            if(phase=="step4")
                            {
                                credentialValue=Environment.GetEnvironmentVariable(name);
                                Require(!String.IsNullOrEmpty(credentialValue)&&Native.Hash(Encoding.UTF8.GetBytes(credentialValue))==S(credential,"sha256"),"Sealed live credential is absent or changed: "+name);
                            }
                            info.EnvironmentVariables[name]=credentialValue;
                        }
                        if(guarded)info.EnvironmentVariables["TRADINGAGENTS_PROTECTION_CONTEXT"]=Convert.ToBase64String(Encoding.UTF8.GetBytes(Evidence.Canonical(ChildContext(prepared,packet,phase))));
                        protection.VerifyProtected();
                        child=children.Start(info,Quote(executable),owner!=null,owner==null?(Action)null:delegate {
                            protection.VerifyProtected();owner.RequireInertLaunch(prepared,packet);
                            var deadlinePipe=pipe;
                            deadline=new ChildDeadline(children,checked(owner.MaximumChildSeconds*1000),delegate { if(deadlinePipe!=null)deadlinePipe.Dispose(); });
                        });starts++;
                        if(guarded) { ReceivePhase(pipe,child,children,protection,prepared,packet,phase,owner,deadline);pipe.Dispose();pipe=null; }
                        if(deadline!=null) { Require(child.WaitForExit(deadline.Remaining),"Conditional inert child exceeded its registered test budget");deadline.Check(); }
                        else if(phase=="guard")Require(child.WaitForExit(10000),"Guard did not exit after owner release");
                        else child.WaitForExit();
                        uint childExit=children.ExitCode(child);
                        Require(childExit==0,"Protected child failed with exit code "+childExit.ToString(CultureInfo.InvariantCulture));
                    }
                }
                catch(Exception error) { failures.Add(error); }
                finally
                {
                    if(deadline!=null)deadline.Dispose();
                    bool exited=false;
                    try { exited=children.Quiesce();if(!exited)failures.Add(new InvalidOperationException("Attempt descendants remain alive")); }
                    catch(Exception error) { failures.Add(error); }
                    if(exited&&protection!=null)
                    { try { protection.Dispose();restored=protection.Restored; } catch(Exception error) { failures.Add(error); } }
                    if(pipe!=null) { try { pipe.Dispose(); } catch(Exception error) { failures.Add(error); } }
                    if(child!=null) { try { child.Dispose(); } catch(Exception error) { failures.Add(error); } }
                    if(dllChanged&&!Native.SetDllDirectoryW(null))failures.Add(new Win32Exception(System.Runtime.InteropServices.Marshal.GetLastWin32Error(),"DLL directory restoration failed"));
                    // If child termination cannot be established, no restoration
                    // runs. Process exit leaves this consumed attempt sealed.
                }
                // A contender that lost exclusive consumption owns no terminal
                // evidence. Only the successful claimant can complete this attempt.
                if(!tree.ConsumedByThisOwner)throw new AggregateException("Protection admission failed; this owner did not consume the attempt",failures);
                var receiptBody=new Dictionary<string,object> {
                    {"contract_version",owner==null?"runtime-protection-completion-v1":ConditionalKind("completion",O(prepared,"attempt"),false)},{"attempt_identity",S(attempt,"attempt_identity")},
                    {"prepared_identity",S(prepared,"identity")},{"packet_identity",S(packet,"identity")},
                    {"state",restored?"COMPLETED_RESTORED":"INCOMPLETE_RETAINED"},{"child_launches",starts},
                    {"exact_baseline_restored",restored},{"failure_count",failures.Count},{"reusable",false} };
                if(owner!=null) { receiptBody.Add("acceptance",inputs["acceptance"]);receiptBody.Add("owner_binding",owner.Binding);receiptBody.Add("execution_authorized",false); }
                var receipt=Evidence.Seal(owner==null?"runtime-protection-completion:v1":ConditionalKind("completion",O(prepared,"attempt"),true),receiptBody);
                Evidence.WriteNew(Path.Combine(S(paths,"evidence_root"),"completion.json"),receipt);
                if(failures.Count>0)throw new AggregateException("Protection attempt failed",failures);
                return receipt;
            }
        }
#endif
        public static int Main(string[] arguments)
        {
#if OWNER_ADMISSION_V2
            Console.Error.WriteLine("ENROLLMENT_ONLY: runtime execution is unavailable in this build");
            return 2;
#else
            try
            {
                Native.InitializePathRuntime();
                Require(IntPtr.Size==8,"64-bit Windows protection host required");
                Require(arguments.Length==3||arguments.Length==5,"Expected operation and pinned input paths/digests");
                using(var input=new PinnedJson(arguments[1],arguments[2]))
                {
                    Dictionary<string,object> result;
                    if(arguments[0]=="initialize"&&arguments.Length==3)result=Initialize(input.Value);
                    else if(arguments[0]=="prepare"&&arguments.Length==3)result=Prepare(input.Value);
                    else if(arguments[0]=="inspect"&&arguments.Length==3)result=Inspect(input.Value);
                    else if(arguments[0]=="run"&&arguments.Length==5)
                    { using(var packet=new PinnedJson(arguments[3],arguments[4]))result=Run(input.Value,packet.Value); }
                    else throw new InvalidOperationException("Unsupported protection operation; no recovery operation exists");
                    Console.WriteLine(Evidence.Canonical(result));return 0;
                }
            }
            catch(Exception error) { Console.Error.WriteLine(error);return 1; }
#endif
        }
    }
}

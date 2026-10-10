// Formal Windows adapter. None of these methods is used by the TEST assembly.
// Activation requires a separately protected v2 registration and platform profile.
using System;
using System.Collections;
using System.Collections.Generic;
using System.ComponentModel;
using System.Diagnostics;
using System.IO;
using System.IO.Pipes;
using System.Runtime.InteropServices;
using System.Security.AccessControl;
using System.Security.Cryptography;
using System.Security.Principal;
using System.ServiceProcess;
using System.Text;
using System.Threading.Tasks;
using Microsoft.Win32;
using Microsoft.Win32.SafeHandles;
using TradingAgents.RuntimeProtection;

namespace TradingAgents.PolicyOwner
{
    internal sealed class AdmissionV2Anchor : IDisposable
    {
        internal readonly Dictionary<string,object> Registration,Binding,Legacy,Cut,Profile;
        internal readonly string Instance,Root,Depot,ServiceSid,OperatorSid,NativePath,PipeName,ControlDirectory;
        internal readonly string RoleDocument;
        readonly List<SafeFileHandle> held=new List<SafeFileHandle>();
        readonly HashSet<string> anchoredDirectories=new HashSet<string>(StringComparer.OrdinalIgnoreCase);
        readonly Dictionary<string,SafeFileHandle> pinnedFiles=new Dictionary<string,SafeFileHandle>(StringComparer.OrdinalIgnoreCase);
        internal AdmissionV2Anchor(string instance)
        {
            Instance=instance;
            string keyPath=OwnerAnchor.RegistryPath(instance);
            using(var machine=RegistryKey.OpenBaseKey(RegistryHive.LocalMachine,RegistryView.Registry64))
            {
                foreach(string path in new[]{@"SOFTWARE\TradingAgents",@"SOFTWARE\TradingAgents\PolicyOwner",keyPath})
                using(var key=machine.OpenSubKey(path,RegistryKeyPermissionCheck.ReadSubTree,RegistryRights.ReadKey|RegistryRights.ReadPermissions))
                {
                    Evidence.Require(key!=null,"Missing protected v2 registration");
                    OwnerAnchor.Descriptor(new RawSecurityDescriptor(key.GetAccessControl(AccessControlSections.Owner|AccessControlSections.Access).GetSecurityDescriptorBinaryForm(),0),"",true);
                    if(path==keyPath)
                    {
                        string json=key.GetValue("AdmissionV2Json",null,RegistryValueOptions.DoNotExpandEnvironmentNames) as string;
                        Evidence.Require(json!=null,"v2 is not activated; legacy fallback is forbidden");
                        Registration=OwnerWire.Parse(Encoding.UTF8.GetBytes(json));
                    }
                }
            }
            Evidence.Closed(Registration,"contract_version","binding","depot_path","cut_record_path","cut_record_pin",
                "platform_profile_path","platform_profile_pin","copies_root","baseline_pins","activation_record_path","activation_record_pin");
            Evidence.Require((string)Registration["contract_version"]=="windows-policy-owner-admission-anchor-v2","Unknown v2 anchor");
            Binding=Evidence.Obj(Registration["binding"]);Legacy=Evidence.Obj(Binding["legacy_setup_binding"]);
            Evidence.Require((string)Binding["contract_version"]=="policy-owner-admission-binding-v2"&&(string)Binding["mode"]==AdmissionV2.Mode,
                "Only enrollment binding is admitted");
            var body=new Dictionary<string,object>(Binding);body.Remove("identity");
            Evidence.Require((string)Binding["identity"]=="policy-owner-admission-binding:v2:sha256:"+Native.Hash(OwnerWire.Bytes(body)),"v2 binding digest mismatch");
            Evidence.Require((string)Legacy["instance"]==instance,"Same-instance mapping mismatch");
            Root=Native.LocalPath((string)Legacy["owner_root"],false);Depot=Native.LocalPath((string)Registration["depot_path"],false);
            Evidence.Require(Depot==Path.Combine(Root,"admission-v2"),"v2 depot must be a new directory within the same instance");
            ServiceSid=(string)Legacy["service_sid"];OperatorSid=(string)Legacy["operator_sid"];
            NativePath=Path.Combine(Depot,"bin","PolicyOwner.exe");PipeName="TA.PolicyOwner.AdmissionV2."+instance;
            ControlDirectory=Path.Combine(Root,"state","admission-v2");
            try
            {
                held.AddRange(Native.Anchor(Root));
                CheckObject(Root,(string)Legacy["owner_root_object_id"],Evidence.Obj(Legacy["owner_root_descriptor_pin"]));
                CheckObject(Path.Combine(Root,"state","owner.sqlite3"),(string)Legacy["owner_state_object_id"],null);
                CheckObject(Path.Combine(Root,"state","lineage.sqlite3"),(string)Legacy["ledger_object_id"],null);
                foreach(string path in new[]{Depot,ControlDirectory,Path.Combine(Root,"authentication"),Path.Combine(Root,"artifacts")})
                { var handle=Native.Open(path,Native.ReadAttributes|Native.ReadControl,3);held.Add(handle);CheckDescriptor(handle); }
                var implementation=Evidence.Obj(Binding["implementation"]);
                string rolePath=Path.Combine(Root,"role-proposal.json");Hold(rolePath,Evidence.Obj(Evidence.Obj(Binding["business"])["role_pin"]));
                RoleDocument=Encoding.UTF8.GetString(File.ReadAllBytes(rolePath));
                var role=OwnerWire.Parse(Encoding.UTF8.GetBytes(RoleDocument));
                Evidence.Require(Object.Equals(role["identity"],Evidence.Obj(Binding["business"])["role_identity"]),"Original role identity mismatch");
                Hold(NativePath,Evidence.Obj(implementation["native_pin"]));
                string manifestPath=Path.Combine(Depot,"worker-manifest.json");Hold(manifestPath,Evidence.Obj(implementation["worker_manifest_pin"]));
                var manifest=OwnerWire.Parse(File.ReadAllBytes(manifestPath));
                Evidence.Closed(manifest,"contract_version","source_commit","files","execution_authorized");
                Evidence.Require((string)manifest["contract_version"]=="policy-owner-admission-worker-depot-v2"&&!(bool)manifest["execution_authorized"]
                    &&Object.Equals(manifest["source_commit"],implementation["source_commit"]),"Worker/source candidate mismatch");
                var paths=new HashSet<string>(StringComparer.OrdinalIgnoreCase);
                foreach(object item in (IEnumerable)manifest["files"])
                {
                    var entry=Evidence.Obj(item);Evidence.Closed(entry,"relative_path","byte_length","sha256");
                    string relative=(string)entry["relative_path"];
                    Evidence.Require(paths.Add(relative)&&(relative.StartsWith("source/",StringComparison.Ordinal)||relative.StartsWith("python/",StringComparison.Ordinal)
                        ||relative=="bin/PolicyOwner.exe"||relative=="bin/ConditionalRuntimeProtectionHost.exe"),"Worker manifest scope/duplicate");
                    Hold(Native.Child(Depot,relative),new Dictionary<string,object>{{"byte_length",entry["byte_length"]},{"sha256",entry["sha256"]}});
                }
                foreach(string subtree in new[]{"source","python","bin"})
                {
                    var pending=new Queue<string>();pending.Enqueue(Path.Combine(Depot,subtree));
                    while(pending.Count>0)
                    {
                        string directory=pending.Dequeue();HoldDirectory(directory);
                        foreach(string file in Directory.GetFiles(directory))
                            Evidence.Require(paths.Contains(file.Substring(Depot.Length+1).Replace('\\','/')),"Unpinned worker import/runtime file");
                        // A child is checked for reparse/ACL before enumeration.
                        foreach(string child in Directory.GetDirectories(directory))pending.Enqueue(child);
                    }
                }
                foreach(string required in new[]{"python/python.exe","source/scripts/policy_owner/owner_worker.py",
                    "source/tradingagents/policy_owner_admission_v2.py","source/tradingagents/policy_owner_admission_crypto.py",
                    "source/tradingagents/policy_owner_admission_windows.py"})Evidence.Require(paths.Contains(required),"Worker closure incomplete");
                Hold((string)Registration["platform_profile_path"],Evidence.Obj(Registration["platform_profile_pin"]));
                Profile=OwnerWire.Parse(File.ReadAllBytes((string)Registration["platform_profile_path"]));
                AdmissionV2Platform.ValidateProfile(Profile);
                Hold((string)Profile["validation_report_path"],Evidence.Obj(Profile["validation_report_pin"]));
                var platformReport=OwnerWire.Parse(File.ReadAllBytes((string)Profile["validation_report_path"]));
                AdmissionV2Platform.ValidatePlatformReport(Profile,platformReport,Binding);
                foreach(object item in (IEnumerable)platformReport["evidence"])
                {var reference=Evidence.Obj(item);Hold((string)reference["path"],Evidence.Obj(reference["pin"]));}
                Hold((string)Registration["cut_record_path"],Evidence.Obj(Registration["cut_record_pin"]));
                Cut=OwnerWire.Parse(File.ReadAllBytes((string)Registration["cut_record_path"]));
                AssertCut();
                Hold((string)Registration["activation_record_path"],Evidence.Obj(Registration["activation_record_pin"]));
                AdmissionV2Platform.ValidateActivation(OwnerWire.Parse(File.ReadAllBytes((string)Registration["activation_record_path"])),Binding,Cut,NativePath,Instance);
            }
            catch{Dispose();throw;}
        }
        void CheckDescriptor(SafeFileHandle handle)
        { OwnerAnchor.Descriptor(new RawSecurityDescriptor(Native.Descriptor(handle),0),ServiceSid,false); }
        void HoldDirectory(string directory)
        {
            if(!anchoredDirectories.Add(directory))return;
            bool protectedScope=String.Equals(directory,Root,StringComparison.OrdinalIgnoreCase)||directory.StartsWith(Root+"\\",StringComparison.OrdinalIgnoreCase);
            var handle=Native.Open(directory,Native.ReadAttributes|(protectedScope?Native.ReadControl:0),3);held.Add(handle);
            Evidence.Require((Native.Information(handle).Attributes&0x10)!=0,"Pinned parent is not a directory");
            if(protectedScope)CheckDescriptor(handle);
        }
        void CheckObject(string path,string objectId,Dictionary<string,object> descriptorPin)
        {
            var handle=Native.Open(path,Native.ReadAttributes|Native.ReadControl,3);held.Add(handle);CheckDescriptor(handle);
            Evidence.Require(Native.ObjectId(handle)==objectId,"Preserved owner object was replaced");
            if(descriptorPin!=null)Evidence.Require(Native.Hash(Native.Descriptor(handle))==(string)descriptorPin["sha256"],"Original root ACL changed");
        }
        internal void Hold(string path,Dictionary<string,object> pin)
        {
            path=Native.LocalPath(path,false);Evidence.Require(path.StartsWith(Root+"\\",StringComparison.OrdinalIgnoreCase),"v2 protected path escaped instance");
            var parents=new List<string>();string parent=Path.GetDirectoryName(path);
            while(!String.IsNullOrEmpty(parent)){parents.Add(parent);parent=Path.GetDirectoryName(parent);}parents.Reverse();
            foreach(string directory in parents)HoldDirectory(directory);
            SafeFileHandle handle;if(!pinnedFiles.TryGetValue(path,out handle))
            {handle=Native.Open(path,Native.ReadData|Native.ReadAttributes|Native.ReadControl,1);held.Add(handle);pinnedFiles.Add(path,handle);}
            CheckDescriptor(handle);var info=Native.Information(handle);
            long length=((long)info.SizeHigh<<32)|info.SizeLow;
            Evidence.Require(length==Convert.ToInt64(pin["byte_length"])&&Native.HashFile(handle)==(string)pin["sha256"],"Protected v2 bytes changed");
        }
        internal void AssertCut()
        {
            Evidence.Closed(Cut,"contract_version","binding_identity","old_boot_id","new_boot_id","guard_identity","guard_record_path","guard_record_pin",
                "boot_record_path","boot_record_pin","phases","copies_manifest_pin","identity");
            var body=new Dictionary<string,object>(Cut);body.Remove("identity");
            Evidence.Require((string)Cut["identity"]=="policy-owner-admission-maintenance-cut:v2:sha256:"+Native.Hash(OwnerWire.Bytes(body)),"Cut record digest mismatch");
            Evidence.Require((string)Cut["contract_version"]=="policy-owner-admission-maintenance-cut-v2"&&Object.Equals(Cut["binding_identity"],Binding["identity"])
                &&!Object.Equals(Cut["old_boot_id"],Cut["new_boot_id"])&&(string)Cut["new_boot_id"]==AdmissionV2Platform.BootIdentity(Profile),"No matching fresh kernel boundary");
            Evidence.Require(Evidence.Canonical(Cut["phases"])=="[\"GUARD_ESTABLISHED\",\"FRESH_KERNEL_BOOT_OBSERVED\",\"LEGACY_STATE_CLASSIFIED\"]","Incomplete cut protocol");
            Hold((string)Cut["guard_record_path"],Evidence.Obj(Cut["guard_record_pin"]));
            Hold((string)Cut["boot_record_path"],Evidence.Obj(Cut["boot_record_pin"]));
            AdmissionV2Platform.ValidateCutRecords(OwnerWire.Parse(File.ReadAllBytes((string)Cut["guard_record_path"])),
                OwnerWire.Parse(File.ReadAllBytes((string)Cut["boot_record_path"])),Cut,Profile,Binding,NativePath,Instance);
            AdmissionV2Platform.AssertServiceStartup(Instance,NativePath,3);
        }
        public void Dispose(){foreach(var handle in held)handle.Dispose();held.Clear();}
    }
    internal static class AdmissionV2Platform
    {
        [DllImport("kernel32.dll",SetLastError=true)]internal static extern bool GetNamedPipeServerProcessId(SafePipeHandle pipe,out uint pid);
        [DllImport("kernel32.dll",SetLastError=true)]static extern bool GetProcessTimes(IntPtr process,out long creation,out long exit,out long kernel,out long user);
        [DllImport("kernel32.dll",SetLastError=true)]static extern SafeProcessHandle OpenProcess(uint access,bool inherit,uint pid);
        [DllImport("kernel32.dll",CharSet=CharSet.Unicode,SetLastError=true)]static extern bool QueryFullProcessImageNameW(SafeProcessHandle process,uint flags,StringBuilder name,ref uint count);
        [DllImport("advapi32.dll",SetLastError=true)]static extern bool OpenProcessToken(IntPtr process,uint access,out SafeAccessTokenHandle token);
        [DllImport("advapi32.dll",SetLastError=true)]static extern bool SetKernelObjectSecurity(IntPtr handle,uint information,byte[] descriptor);
        [DllImport("advapi32.dll",SetLastError=true)]static extern bool SetTokenInformation(SafeAccessTokenHandle token,int kind,IntPtr value,int length);
        [DllImport("kernel32.dll",CharSet=CharSet.Unicode,SetLastError=true)]static extern IntPtr LoadLibraryExW(string name,IntPtr file,uint flags);
        [DllImport("kernel32.dll",CharSet=CharSet.Ansi,SetLastError=true)]static extern IntPtr GetProcAddress(IntPtr module,string name);
        [DllImport("kernel32.dll")]static extern bool FreeLibrary(IntPtr module);
        [UnmanagedFunctionPointer(CallingConvention.Winapi)]delegate int QuerySystem(int kind,IntPtr buffer,int length,out int returned);
        [UnmanagedFunctionPointer(CallingConvention.Winapi)]delegate int QueryVersion(IntPtr buffer);
        internal static string OsVersion()
        {
            IntPtr library=LoadLibraryExW("ntdll.dll",IntPtr.Zero,0x800),buffer=Marshal.AllocHGlobal(284);
            Evidence.Require(library!=IntPtr.Zero,"OS version API unavailable");
            try
            {
                for(int i=0;i<284;i++)Marshal.WriteByte(buffer,i,0);Marshal.WriteInt32(buffer,284);
                IntPtr address=GetProcAddress(library,"RtlGetVersion");Evidence.Require(address!=IntPtr.Zero,"OS version API missing");
                var query=(QueryVersion)Marshal.GetDelegateForFunctionPointer(address,typeof(QueryVersion));Evidence.Require(query(buffer)==0,"OS version query failed");
                return Marshal.ReadInt32(buffer,4)+"."+Marshal.ReadInt32(buffer,8)+"."+Marshal.ReadInt32(buffer,12);
            }
            finally{Marshal.FreeHGlobal(buffer);FreeLibrary(library);}
        }
        internal static void ValidateProfile(Dictionary<string,object> profile)
        {
            Evidence.Closed(profile,"contract_version","os_version","architecture","boot_information_class","boot_information_size","parent_information_class","parent_information_size","validation_report_path","validation_report_pin","identity");
            Evidence.Require((string)profile["contract_version"]=="policy-owner-admission-platform-profile-v2"&&(string)profile["architecture"]=="x64"
                &&Convert.ToInt32(profile["boot_information_class"])==90&&Convert.ToInt32(profile["boot_information_size"])==32
                &&Convert.ToInt32(profile["parent_information_class"])==0&&Convert.ToInt32(profile["parent_information_size"])==48
                &&(string)profile["os_version"]==OsVersion(),"Unqualified Windows platform profile");
            var body=new Dictionary<string,object>(profile);body.Remove("identity");
            Evidence.Require((string)profile["identity"]=="policy-owner-admission-platform-profile:v2:sha256:"+Native.Hash(OwnerWire.Bytes(body)),"Platform profile digest mismatch");
        }
        internal static void ValidatePlatformReport(Dictionary<string,object> profile,Dictionary<string,object> report,Dictionary<string,object> binding)
        {
            Evidence.Closed(report,"contract_version","source_commit","native_pin","os_version","result","controls","evidence","execution_authorized","identity");
            var body=new Dictionary<string,object>(report);body.Remove("identity");var implementation=Evidence.Obj(binding["implementation"]);
            Evidence.Require((string)report["identity"]=="policy-owner-admission-platform-validation:v2:sha256:"+Native.Hash(OwnerWire.Bytes(body))
                &&(string)report["contract_version"]=="policy-owner-admission-platform-validation-v2"&&(string)report["result"]=="PLATFORM_VALIDATION_PASSED"
                &&!(bool)report["execution_authorized"]&&Object.Equals(report["source_commit"],implementation["source_commit"])
                &&Evidence.Canonical(report["native_pin"])==Evidence.Canonical(implementation["native_pin"])&&Object.Equals(report["os_version"],profile["os_version"]),"Missing bound platform validation report");
            var controls=new HashSet<string>(StringComparer.Ordinal);foreach(object value in (IEnumerable)report["controls"])Evidence.Require(controls.Add((string)value),"Duplicate platform control");
            Evidence.Require(controls.SetEquals(new[]{"full-kernel-boot-vs-resume","disabled-startup-across-boot","service-sid-and-token","registry-and-file-acl",
                "lease-duplicate-handle-lifetime","owned-worker-parent-query","webauthn-user-handle","sqlite-durability-and-cut-copies"}),"Platform validation coverage incomplete");
            int count=0;foreach(object item in (IEnumerable)report["evidence"]){var value=Evidence.Obj(item);Evidence.Closed(value,"path","pin");count++;}
            Evidence.Require(count>=8,"Raw platform evidence references missing");
        }
        internal static void ValidateCutRecords(Dictionary<string,object> guard,Dictionary<string,object> boot,Dictionary<string,object> cut,
            Dictionary<string,object> profile,Dictionary<string,object> binding,string nativePath,string instance)
        {
            Evidence.Closed(guard,"contract_version","binding_identity","legacy_binding_identity","profile_identity","old_boot_id","disabled_startup","recorded_at_utc","identity");
            Evidence.Closed(boot,"contract_version","binding_identity","guard_identity","new_boot_id","disabled_startup","recorded_at_utc","identity");
            var guardBody=new Dictionary<string,object>(guard);guardBody.Remove("identity");var bootBody=new Dictionary<string,object>(boot);bootBody.Remove("identity");
            Evidence.Require((string)guard["contract_version"]=="policy-owner-admission-startup-guard-v2"&&(string)boot["contract_version"]=="policy-owner-admission-fresh-boot-v2"
                &&(string)guard["identity"]=="policy-owner-admission-startup-guard:v2:sha256:"+Native.Hash(OwnerWire.Bytes(guardBody))
                &&(string)boot["identity"]=="policy-owner-admission-fresh-boot:v2:sha256:"+Native.Hash(OwnerWire.Bytes(bootBody)),"Raw guard/boot receipt invalid");
            Evidence.Require(Object.Equals(guard["identity"],cut["guard_identity"])&&Object.Equals(guard["identity"],boot["guard_identity"])
                &&Object.Equals(guard["binding_identity"],binding["identity"])&&Object.Equals(boot["binding_identity"],binding["identity"])
                &&Object.Equals(guard["legacy_binding_identity"],Evidence.Obj(binding["legacy_setup_binding"])["identity"])
                &&Object.Equals(guard["profile_identity"],profile["identity"])&&Object.Equals(guard["old_boot_id"],cut["old_boot_id"])
                &&Object.Equals(boot["new_boot_id"],cut["new_boot_id"]),"Raw cut receipt chain mismatch");
            var expected=StartupDocument(instance,nativePath,4);
            Evidence.Require(Evidence.Canonical(guard["disabled_startup"])==Evidence.Canonical(expected)&&Evidence.Canonical(boot["disabled_startup"])==Evidence.Canonical(expected),"Disabled startup was not preserved through cut");
        }
        internal static Dictionary<string,object> StartupDocument(string instance,string image,int start)
        {return new Dictionary<string,object>{{"image_path","\""+image+"\" service-v2 "+instance},{"start",start},{"type",16},
            {"account",@"NT SERVICE\TA.PolicyOwner."+instance},{"failure_command",""},{"delayed_auto_start",0},{"trigger_info",null}};}
        internal static void ValidateActivation(Dictionary<string,object> value,Dictionary<string,object> binding,Dictionary<string,object> cut,string image,string instance)
        {
            Evidence.Closed(value,"contract_version","binding_identity","cut_identity","migration_phase","gate","attempt","startup","recorded_at_utc","identity");
            var body=new Dictionary<string,object>(value);body.Remove("identity");
            Evidence.Require((string)value["identity"]=="policy-owner-admission-activation:v2:sha256:"+Native.Hash(OwnerWire.Bytes(body))
                &&(string)value["contract_version"]=="policy-owner-admission-activation-v2"&&Object.Equals(value["binding_identity"],binding["identity"])
                &&Object.Equals(value["cut_identity"],cut["identity"])&&(string)value["migration_phase"]=="V2_ACTIVATED_CLOSED"
                &&(string)value["gate"]=="CLOSED"&&value["attempt"]==null&&Evidence.Canonical(value["startup"])==Evidence.Canonical(StartupDocument(instance,image,3)),
                "Final durable activation receipt is absent or inconsistent; no staged-registration fallback");
        }
        internal static string BootIdentity(Dictionary<string,object> profile)
        {
            ValidateProfile(profile);IntPtr library=LoadLibraryExW("ntdll.dll",IntPtr.Zero,0x800);Evidence.Require(library!=IntPtr.Zero,"Kernel boot API unavailable");
            IntPtr buffer=Marshal.AllocHGlobal(32);
            try
            {
                IntPtr address=GetProcAddress(library,"NtQuerySystemInformation");Evidence.Require(address!=IntPtr.Zero,"Kernel boot API missing");
                var query=(QuerySystem)Marshal.GetDelegateForFunctionPointer(address,typeof(QuerySystem));int returned;
                Evidence.Require(query(90,buffer,32,out returned)==0&&returned==32,"Kernel boot layout/status unqualified");
                byte[] bytes=new byte[16];Marshal.Copy(buffer,bytes,0,16);var id=new Guid(bytes);
                Evidence.Require(id!=Guid.Empty,"Empty kernel boot identity");
                // Refuse hibernation/soft-boot flags in this profile. Platform
                // validation must establish the exact supported OS semantics.
                ulong flags=unchecked((ulong)Marshal.ReadInt64(buffer,24));Evidence.Require((flags&6)==0,"Fresh full kernel boot required");return id.ToString("D");
            }
            finally{Marshal.FreeHGlobal(buffer);FreeLibrary(library);}
        }
        internal static void AssertServiceStartup(string instance,string image,int requiredStart)
        {
            using(var machine=RegistryKey.OpenBaseKey(RegistryHive.LocalMachine,RegistryView.Registry64))
            using(var key=machine.OpenSubKey(@"SYSTEM\CurrentControlSet\Services\TA.PolicyOwner."+instance,RegistryKeyPermissionCheck.ReadSubTree,RegistryRights.ReadKey|RegistryRights.ReadPermissions))
            {
                Evidence.Require(key!=null,"Registered service startup guard absent");
                OwnerAnchor.Descriptor(new RawSecurityDescriptor(key.GetAccessControl(AccessControlSections.Owner|AccessControlSections.Access).GetSecurityDescriptorBinaryForm(),0),"",true);
                string expected="\""+image+"\" service-v2 "+instance;
                Evidence.Require(Object.Equals(key.GetValue("ImagePath",null,RegistryValueOptions.DoNotExpandEnvironmentNames),expected)
                    &&Convert.ToInt32(key.GetValue("Start",-1))==requiredStart&&Convert.ToInt32(key.GetValue("Type",-1))==16
                    &&Object.Equals(key.GetValue("ObjectName",null),@"NT SERVICE\TA.PolicyOwner."+instance),"Service configuration can still start a different implementation");
                Evidence.Require(String.IsNullOrEmpty(key.GetValue("FailureCommand","") as string)&&Convert.ToInt32(key.GetValue("DelayedAutoStart",0))==0,"Uncontrolled service recovery/startup command");
                using(var triggers=key.OpenSubKey("TriggerInfo"))Evidence.Require(triggers==null,"Unqualified service trigger configuration");
            }
        }
        internal static long CreationTime(IntPtr process)
        {long creation,exit,kernel,user;if(!GetProcessTimes(process,out creation,out exit,out kernel,out user))throw new Win32Exception(Marshal.GetLastWin32Error());return creation;}
        internal static void ExposeOwnProcess(AdmissionV2Anchor anchor,bool frontend)
        {
            using(var identity=WindowsIdentity.GetCurrent())using(var process=Process.GetCurrentProcess())
            {
                Evidence.Require(identity.User.Value==(frontend?anchor.OperatorSid:anchor.ServiceSid),"Wrong current native principal");
                if(frontend)Evidence.Require(Native.TokenHasEnabledSid(identity.AccessToken,"S-1-5-32-544")&&OwnerWire.IntegrityLevel(identity.AccessToken)>=0x3000,"Elevated registered frontend required");
                string principal=identity.User.Value,reader=frontend?anchor.ServiceSid:anchor.OperatorSid;
                string grants="D:P(D;;WDWO;;;OW)(A;;GA;;;SY)(A;;GA;;;BA)(A;;GA;;;"+principal+")";
                SafeAccessTokenHandle token;if(!OpenProcessToken(process.Handle,0x000e008b,out token))throw new Win32Exception(Marshal.GetLastWin32Error());
                using(token)
                {
                    if(!frontend)
                    {
                        var descriptor=new RawSecurityDescriptor("D:P(A;;GA;;;SY)(A;;GA;;;BA)(A;;GA;;;"+anchor.ServiceSid+")");
                        byte[] bytes=new byte[descriptor.DiscretionaryAcl.BinaryLength];descriptor.DiscretionaryAcl.GetBinaryForm(bytes,0);
                        var pin=GCHandle.Alloc(bytes,GCHandleType.Pinned);IntPtr pointer=Marshal.AllocHGlobal(IntPtr.Size);
                        try{Marshal.WriteIntPtr(pointer,pin.AddrOfPinnedObject());if(!SetTokenInformation(token,6,pointer,IntPtr.Size))throw new Win32Exception(Marshal.GetLastWin32Error());}
                        finally{Marshal.FreeHGlobal(pointer);pin.Free();}
                    }
                    if(!SetKernelObjectSecurity(token.DangerousGetHandle(),0x80000004,Native.FromSddl(grants+"(A;;0x00020008;;;"+reader+")")))throw new Win32Exception(Marshal.GetLastWin32Error());
                }
                if(!SetKernelObjectSecurity(process.Handle,0x80000004,Native.FromSddl(grants+"(A;;0x00101000;;;"+reader+")")))throw new Win32Exception(Marshal.GetLastWin32Error());
            }
        }
        internal static Dictionary<string,object> ProcessIdentity(uint pid,AdmissionV2Anchor anchor,bool frontend)
        {
            using(var process=OpenProcess(0x1000,false,pid))
            {
                Evidence.Require(!process.IsInvalid,"Actual process handle unavailable");var name=new StringBuilder(32768);uint count=(uint)name.Capacity;
                if(!QueryFullProcessImageNameW(process,0,name,ref count))throw new Win32Exception(Marshal.GetLastWin32Error());
                Evidence.Require(String.Equals(name.ToString(),anchor.NativePath,StringComparison.OrdinalIgnoreCase),"Actual pipe process image mismatch");
                SafeAccessTokenHandle token;if(!OpenProcessToken(process.DangerousGetHandle(),8,out token))throw new Win32Exception(Marshal.GetLastWin32Error());
                using(token)using(var identity=new WindowsIdentity(token.DangerousGetHandle()))
                {
                    Evidence.Require(identity.User.Value==(frontend?anchor.OperatorSid:anchor.ServiceSid),"Actual pipe principal mismatch");
                    if(frontend)Evidence.Require(Native.TokenHasEnabledSid(token,"S-1-5-32-544")&&OwnerWire.IntegrityLevel(token)>=0x3000,"Frontend is not elevated");
                }
                return new Dictionary<string,object>{{"pid",pid},{"creation_filetime",CreationTime(process.DangerousGetHandle())},
                    {"image_pin",Evidence.Obj(Evidence.Obj(anchor.Binding["implementation"])["native_pin"])}};
            }
        }
        internal static string Nonce(){byte[] value=new byte[32];using(var random=RandomNumberGenerator.Create())random.GetBytes(value);return BitConverter.ToString(value).Replace("-","").ToLowerInvariant();}
        internal static void WriteNew(string path,byte[] raw)
        {using(var stream=new FileStream(path,FileMode.CreateNew,FileAccess.Write,FileShare.Read,4096,FileOptions.WriteThrough)){stream.Write(raw,0,raw.Length);stream.Flush(true);}}
    }
    internal sealed class AdmissionV2Rpc : IAdmissionRpc
    {
        readonly AdmissionV2Anchor anchor;
        internal AdmissionV2Rpc(AdmissionV2Anchor value){anchor=value;}
        public Dictionary<string,object> Call(string operation,Dictionary<string,object> payload)
        {
            Evidence.Require(AdmissionV2.RpcOperation(operation),"No generic RPC is exposed");anchor.AssertCut();
            var request=new Dictionary<string,object>{{"contract_version","policy-owner-admission-rpc-v2"},{"instance",anchor.Instance},
                {"nonce",AdmissionV2Platform.Nonce()},{"operation",operation},{"payload",payload}};
            using(var pipe=new NamedPipeClientStream(".",anchor.PipeName,PipeDirection.InOut,PipeOptions.Asynchronous,TokenImpersonationLevel.Identification))
            {
                pipe.Connect(10000);uint pid;if(!AdmissionV2Platform.GetNamedPipeServerProcessId(pipe.SafePipeHandle,out pid))throw new Win32Exception(Marshal.GetLastWin32Error());
                AdmissionV2Platform.ProcessIdentity(pid,anchor,false);
                var task=Task.Run(delegate{OwnerWire.Write(pipe,request);var response=OwnerWire.Read(pipe);OwnerWire.Acknowledge(pipe,request,response);return response;});
                Evidence.Require(task.Wait(60000),"v2 RPC deadline exceeded; no retry");var result=task.Result;
                Evidence.Closed(result,"contract_version","nonce","request_sha256","ok","result","error");
                Evidence.Require((string)result["contract_version"]=="policy-owner-admission-response-v2"&&Object.Equals(result["nonce"],request["nonce"])
                    &&(string)result["request_sha256"]==Native.Hash(OwnerWire.Bytes(request)),"v2 response binding mismatch");
                Evidence.Require((bool)result["ok"],"v2 rejected: "+(string)result["error"]);return Evidence.Obj(result["result"]);
            }
        }
    }
    internal sealed class AdmissionV2Service : ServiceBase
    {
        readonly string instance;volatile bool stopping;NamedPipeServerStream listening;FileStream lease,gate;
        readonly object sync=new object();ProtectedChildren active;string leaseId,creationReceipt;Dictionary<string,object> challenge;
        [StructLayout(LayoutKind.Sequential)]struct SecurityAttributes{internal int Length;internal IntPtr Descriptor;internal int Inherit;}
        [DllImport("kernel32.dll",CharSet=CharSet.Unicode,SetLastError=true)]static extern SafePipeHandle CreateNamedPipeW(string name,uint mode,uint pipeMode,uint instances,uint outputSize,uint inputSize,uint timeout,ref SecurityAttributes attributes);
        [DllImport("kernel32.dll",SetLastError=true)]static extern bool CancelIoEx(SafePipeHandle pipe,IntPtr overlapped);
        internal AdmissionV2Service(string value){instance=value;ServiceName="TA.PolicyOwner."+value;CanStop=true;AutoLog=false;}
        protected override void OnStart(string[] args){Task.Run(delegate{try{Serve();}catch(Exception error){Trace.WriteLine(error);Stop();}});}
        protected override void OnStop(){stopping=true;lock(sync){if(active!=null)active.Terminate();}if(listening!=null)listening.Dispose();}
        NamedPipeServerStream Pipe(AdmissionV2Anchor anchor)
        {
            byte[] descriptor=Native.FromSddl("D:P(A;;GA;;;SY)(A;;GA;;;BA)(A;;GA;;;"+anchor.ServiceSid+")(A;;GRGW;;;"+anchor.OperatorSid+")");
            var pin=GCHandle.Alloc(descriptor,GCHandleType.Pinned);
            try{var sa=new SecurityAttributes{Length=Marshal.SizeOf(typeof(SecurityAttributes)),Descriptor=pin.AddrOfPinnedObject()};
                var handle=CreateNamedPipeW(@"\\.\pipe\"+anchor.PipeName,0x40080003,8,1,65536,65536,10000,ref sa);
                if(handle.IsInvalid)throw new Win32Exception(Marshal.GetLastWin32Error());return new NamedPipeServerStream(PipeDirection.InOut,true,false,handle);}
            finally{pin.Free();}
        }
        void Serve()
        {
            using(var anchor=new AdmissionV2Anchor(instance))using(var identity=WindowsIdentity.GetCurrent())
            {
                Evidence.Require(identity.User.Value==anchor.ServiceSid,"Dedicated owner service account required");
                Evidence.Require(String.Equals(Process.GetCurrentProcess().MainModule.FileName,anchor.NativePath,StringComparison.OrdinalIgnoreCase),"Unregistered v2 service image");
                AdmissionV2Platform.ExposeOwnProcess(anchor,false);
                gate=new FileStream(Path.Combine(anchor.ControlDirectory,"owner-gate.lock"),FileMode.Open,FileAccess.ReadWrite,FileShare.Read);
                leaseId=AdmissionV2Platform.Nonce();lease=new FileStream(Path.Combine(anchor.ControlDirectory,"lease-"+leaseId+".json"),FileMode.CreateNew,FileAccess.ReadWrite,FileShare.Read,4096,FileOptions.WriteThrough);
                var leaseRecord=new Dictionary<string,object>{{"contract_version","policy-owner-admission-lease-v2"},{"lease_id",leaseId},{"binding_identity",anchor.Binding["identity"]},
                    {"holder",AdmissionV2Platform.ProcessIdentity((uint)Process.GetCurrentProcess().Id,anchor,false)},
                    {"holder_handles",new Dictionary<string,object>{{"gate",gate.SafeFileHandle.DangerousGetHandle().ToInt64()},{"lease",lease.SafeFileHandle.DangerousGetHandle().ToInt64()}}}};
                byte[] raw=OwnerWire.Bytes(leaseRecord);lease.Write(raw,0,raw.Length);lease.Flush(true);
                try
                {
                    // Reopening with a different service generation asks the
                    // core to mark every nonterminal phase BLOCKED before RPC.
                    var initial=Worker(anchor,"inspect-v2",new Dictionary<string,object>());
                    if((string)initial["phase"]=="CLOSED"&&(string)initial["classification"]=="ELIGIBLE"&&(string)initial["migration_phase"]=="V2_ACTIVATED_CLOSED"&&initial["attempt"]==null)
                        Worker(anchor,"adopt-closed-v2",new Dictionary<string,object>{{"expected_epoch",initial["epoch"]}});
                    using(var pipe=Pipe(anchor))
                    {
                        listening=pipe;
                        while(!stopping)
                        {
                            Task pending=null;
                            try
                            {
                                pipe.WaitForConnection();var read=Task.Run(delegate{return OwnerWire.Read(pipe);});pending=read;
                                Evidence.Require(read.Wait(10000),"v2 input framing timeout");var request=read.Result;pending=null;
                                var response=new Dictionary<string,object>{{"contract_version","policy-owner-admission-response-v2"},
                                    {"nonce",request.ContainsKey("nonce")?request["nonce"]:""},{"request_sha256",Native.Hash(OwnerWire.Bytes(request))},{"ok",false},{"result",null},{"error","Rejected"}};
                                try
                                {
                                    Evidence.Closed(request,"contract_version","instance","nonce","operation","payload");
                                    Evidence.Require((string)request["contract_version"]=="policy-owner-admission-rpc-v2"&&(string)request["instance"]==instance
                                        &&System.Text.RegularExpressions.Regex.IsMatch((string)request["nonce"],"\\A[0-9a-f]{64}\\z"),"v2 RPC framing identity");
                                    string operation=(string)request["operation"];Evidence.Require(AdmissionV2.RpcOperation(operation),"Legacy/migration/generic RPC denied before state");
                                    string caller=null;pipe.RunAsClient(delegate{using(var callerIdentity=WindowsIdentity.GetCurrent(true)){Evidence.Require(callerIdentity!=null,"Missing pipe token");caller=callerIdentity.User.Value;}});
                                    Evidence.Require(caller==anchor.OperatorSid,"Wrong pipe principal");uint pid;if(!OwnerWire.GetNamedPipeClientProcessId(pipe.SafePipeHandle,out pid))throw new Win32Exception(Marshal.GetLastWin32Error());
                                    var frontend=AdmissionV2Platform.ProcessIdentity(pid,anchor,true);var payload=Evidence.Obj(request["payload"]);
                                    Evidence.Require(!payload.ContainsKey("frontend"),"Frontend identity is supplied by the OS, never the payload");
                                    if(operation=="reserve-v2"||operation=="begin-creation-v2"||operation=="accept-v2")payload.Add("frontend",frontend);
                                    if(operation=="accept-v2")
                                    {
                                        Evidence.Require(challenge!=null,"No owned active challenge");var registration=Evidence.Obj(payload["registration"]);var proof=Evidence.Obj(payload["proof"]);
                                        var nativeProof=new WindowsPolicyAuthentication.Proof{CredentialId=Convert.FromBase64String((string)proof["credential_id"]),AuthenticatorData=Convert.FromBase64String((string)proof["authenticator_data"]),
                                            Signature=Convert.FromBase64String((string)proof["signature"]),ClientData=Convert.FromBase64String((string)proof["client_data"])};
                                        byte[] id=Convert.FromBase64String((string)registration["id"]);byte[] key=WindowsPolicyAuthentication.RegistrationPublicKey((string)challenge["rp_id"],Convert.FromBase64String((string)registration["authenticator_data"]),id);
                                        Evidence.Require(WindowsPolicyAuthentication.Equal(key,Convert.FromBase64String((string)registration["public_key"])),"Registration key mismatch");
                                        WindowsPolicyAuthentication.Verify((string)challenge["rp_id"],Convert.FromBase64String((string)challenge["challenge"]),id,key,0,nativeProof);
                                    }
                                    Dictionary<string,object> result;
                                    if(operation=="creation-boundary-v2")
                                    {
                                        Evidence.Closed(payload,"expected_epoch","attempt","creation_receipt","parameters");
                                        Evidence.Require(challenge!=null&&creationReceipt!=null&&Object.Equals(payload["creation_receipt"],creationReceipt)
                                            &&Convert.ToInt64(payload["expected_epoch"])==Convert.ToInt64(challenge["epoch"])&&Object.Equals(payload["attempt"],challenge["attempt_id"]),"Boundary attempt mismatch");
                                        var state=Worker(anchor,"inspect-v2",new Dictionary<string,object>());
                                        Evidence.Require((string)state["phase"]=="CREATION_BEGUN"&&Convert.ToInt64(state["epoch"])==Convert.ToInt64(challenge["epoch"]),"Creation boundary is no longer current");
                                        Evidence.Require(Evidence.Canonical(frontend)==Evidence.Canonical(Evidence.Obj(challenge["challenge_document"])["frontend"]),"Boundary frontend generation mismatch");
                                        var expected=new Dictionary<string,object>{{"rp",challenge["rp_id"]},{"user_id",challenge["user_handle"]},{"user_size",32},{"name",anchor.OperatorSid},
                                            {"client_data",Convert.ToBase64String(WindowsPolicyAuthentication.ClientJson((string)challenge["rp_id"],Convert.FromBase64String((string)challenge["challenge"]),true))},
                                            {"attachment",1},{"verification",1},{"attestation",1},{"algorithm",-7},{"window_nonzero",true},{"boundary","MARSHALLED_BEFORE_CALL"}};
                                        Evidence.Require(Evidence.Canonical(payload["parameters"])==Evidence.Canonical(expected),"Marshalled API parameters differ from the reserved generation");
                                        var parameters=new Dictionary<string,object>{{"contract_version","policy-owner-admission-creation-parameters-v2"},{"lease_id",leaseId},
                                            {"binding_identity",anchor.Binding["identity"]},{"creation_receipt",creationReceipt},{"frontend",frontend},{"challenge",challenge},{"boundary_parameters",expected}};
                                        string digest=Native.Hash(Encoding.UTF8.GetBytes(creationReceipt));
                                        AdmissionV2Platform.WriteNew(Path.Combine(anchor.ControlDirectory,"creation-"+digest+".json"),OwnerWire.Bytes(parameters));
                                        result=new Dictionary<string,object>{{"status","BOUNDARY_RECORDED"},{"creation_receipt",creationReceipt},{"execution_authorized",false}};
                                    }
                                    else result=Worker(anchor,operation,payload);
                                    if(operation=="reserve-v2")challenge=result;
                                    if(operation=="begin-creation-v2")
                                    {
                                        Evidence.Require(challenge!=null,"Missing owned reservation");creationReceipt=(string)result["creation_receipt"];
                                    }
                                    response["ok"]=true;response["result"]=result;response["error"]=null;
                                }
                                catch(Exception error){response["error"]=error.GetType().Name+": "+error.Message;}
                                var send=Task.Run(delegate{OwnerWire.Write(pipe,response);OwnerWire.ReadAcknowledgement(pipe,request,response);});pending=send;
                                Evidence.Require(send.Wait(10000),"v2 response/ack timeout");pending=null;
                            }
                            catch(Exception error){if(stopping)break;Trace.WriteLine(error);}
                            finally
                            {
                                if(pending!=null&&!pending.IsCompleted)CancelIoEx(pipe.SafePipeHandle,IntPtr.Zero);
                                if(!stopping&&pipe.IsConnected)pipe.Disconnect();
                                if(pending!=null){try{pending.Wait(5000);}catch(AggregateException){}Evidence.Require(pending.IsCompleted,"v2 pending I/O did not quiesce");}
                            }
                        }
                    }
                }
                finally{listening=null;lease.Dispose();gate.Dispose();lease=null;gate=null;}
            }
        }
        Dictionary<string,object> Worker(AdmissionV2Anchor anchor,string operation,Dictionary<string,object> payload)
        {
            Evidence.Require(AdmissionV2.WorkerOperation(operation)||operation=="adopt-closed-v2","Generic worker dispatch absent");anchor.AssertCut();
            var request=new Dictionary<string,object>{{"contract_version","policy-owner-trusted-worker-request-v2"},{"mode",AdmissionV2.Mode},
                {"anchor_v2",anchor.Binding},{"lease_id",leaseId},{"operation",operation},{"payload",payload}};
            var start=new ProcessStartInfo(Path.Combine(anchor.Depot,"python","python.exe")){Arguments="-I -B -S \""+Path.Combine(anchor.Depot,"source","scripts","policy_owner","owner_worker.py")+"\"",
                UseShellExecute=false,CreateNoWindow=true,RedirectStandardInput=true,RedirectStandardOutput=true,RedirectStandardError=true,WorkingDirectory=Path.Combine(anchor.Root,"state")};
            start.EnvironmentVariables.Clear();string windows=Environment.GetFolderPath(Environment.SpecialFolder.Windows);start.EnvironmentVariables["SystemRoot"]=windows;
            start.EnvironmentVariables["PATH"]=Path.Combine(anchor.Depot,"python")+";"+Path.Combine(windows,"System32");
            start.EnvironmentVariables["TEMP"]=Path.Combine(anchor.Root,"state","temp");start.EnvironmentVariables["TMP"]=start.EnvironmentVariables["TEMP"];
            using(var children=new ProtectedChildren(256L*1024*1024))
            {lock(sync){Evidence.Require(!stopping&&active==null,"v2 worker unavailable");active=children;}
                try{return OwnerWire.Parse(children.RunRedirected(start,"\""+start.FileName+"\"",OwnerWire.Bytes(request),45000));}
                finally{lock(sync)active=null;}}
        }
    }
}

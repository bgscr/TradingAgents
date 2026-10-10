// Fresh additive registration, available only through a visible human setup.
// Setup creates no role authentication, approval, allocation, claim or clock.
using System;
using System.Collections;
using System.Collections.Generic;
using System.ComponentModel;
using System.IO;
using System.Runtime.InteropServices;
using System.Security.AccessControl;
using System.Security.Principal;
using System.Windows.Forms;
using Microsoft.Win32;
using Microsoft.Win32.SafeHandles;
using TradingAgents.RuntimeProtection;

namespace TradingAgents.PolicyOwner
{
    internal static class PolicyOwnerSetup
    {
        [StructLayout(LayoutKind.Sequential)] struct CreateAttributes {internal int Length;internal IntPtr Descriptor;internal int Inherit;}
        [DllImport("kernel32.dll",CharSet=CharSet.Unicode,SetLastError=true)] static extern bool CreateDirectoryW(string path,ref CreateAttributes security);
        [DllImport("advapi32.dll",CharSet=CharSet.Unicode,SetLastError=true)] static extern IntPtr OpenSCManagerW(string machine,string database,uint access);
        [DllImport("advapi32.dll",CharSet=CharSet.Unicode,SetLastError=true)] static extern IntPtr OpenServiceW(IntPtr manager,string name,uint access);
        [DllImport("advapi32.dll",CharSet=CharSet.Unicode,SetLastError=true)] static extern IntPtr CreateServiceW(IntPtr manager,string name,string display,uint access,uint type,uint start,uint error,string command,string group,IntPtr tag,string dependencies,string account,string password);
        [DllImport("advapi32.dll",SetLastError=true)] static extern bool CloseServiceHandle(IntPtr handle);
        [DllImport("advapi32.dll",SetLastError=true)] static extern bool SetServiceObjectSecurity(IntPtr service,uint information,byte[] descriptor);
        [DllImport("advapi32.dll",SetLastError=true)] static extern bool StartServiceW(IntPtr service,uint count,IntPtr arguments);

        static string Dacl(string service,string principal,bool mutable)
        {
            return "O:BAG:BAD:P"+(mutable?"(D;OICI;WDWO;;;OW)":"")
                +"(A;OICI;FA;;;SY)(A;OICI;FA;;;BA)(A;OICI;"+(mutable?"0x001301bf":"FRFX")+";;;"+service+")"
                +"(A;OICI;FRFX;;;"+principal+")";
        }
        static void FileAcl(string path,string sddl)
        { var security=new FileSecurity();security.SetSecurityDescriptorSddlForm(sddl);File.SetAccessControl(path,security); }
        static void DirectoryAcl(string path,string sddl)
        { var security=new DirectorySecurity();security.SetSecurityDescriptorSddlForm(sddl);Directory.SetAccessControl(path,security); }
        static Dictionary<string,object> Pin(string path)
        {
            using(var file=Native.Open(path,Native.ReadData|Native.ReadAttributes|Native.ReadControl,1))
                return new Dictionary<string,object>{{"sha256",Native.HashFile(file)},{"byte_length",new FileInfo(path).Length}};
        }
        static string Id(string path)
        { using(var file=Native.Open(path,Native.ReadAttributes|Native.ReadControl,3))return Native.ObjectId(file); }
        static bool Mutable(string relative)
        { return relative=="state"||relative.StartsWith("state/",StringComparison.Ordinal)||relative=="authentication"||relative.StartsWith("authentication/",StringComparison.Ordinal)||relative=="artifacts"||relative.StartsWith("artifacts/",StringComparison.Ordinal); }

        internal static Dictionary<string,object> Interactive(string proposalPath,string proposalSha)
        {
            using(var proposal=new PinnedJson(proposalPath,proposalSha))
            using(var identity=WindowsIdentity.GetCurrent())
            {
                var value=proposal.Value;
                Evidence.CheckIdentity(value,"policy-owner-setup-proposal:v1");
                Evidence.Closed(value,"contract_version","instance","root","operator_sid","observed_account_name","service_sid","role_proposal","ledger_identity","files","mechanism","registration_is_approval","execution_authorized","identity");
                Evidence.Require((string)value["contract_version"]=="policy-owner-setup-proposal-v1"
                    &&(string)value["mechanism"]=="WINDOWS_HELLO_WEBAUTHN_AND_DEDICATED_VIRTUAL_SERVICE_ACCOUNT"
                    &&!(bool)value["registration_is_approval"]&&!(bool)value["execution_authorized"],"Unknown setup contract");
                Evidence.Require(identity.User.Value==(string)value["operator_sid"]&&Native.TokenHasEnabledSid(identity.AccessToken,"S-1-5-32-544")
                    &&OwnerWire.IntegrityLevel(identity.AccessToken)>=0x3000,"The exact proposed operator must run the visible setup elevated");
                string instance=(string)value["instance"],registryPath=OwnerAnchor.RegistryPath(instance);
                string root=Native.LocalPath((string)value["root"],false);
                string expected=Path.Combine(Environment.GetFolderPath(Environment.SpecialFolder.CommonApplicationData),"TA-PolicyOwner-"+instance);
                Evidence.Require(root==expected,"Setup root must be the exact fresh per-instance ProgramData directory");
                Evidence.Require(!Directory.Exists(root)&&!File.Exists(root),"Setup never replaces or repairs an existing owner root");
                var role=Evidence.Obj(value["role_proposal"]);
                Evidence.CheckIdentity(role,"policy-owner-project-role-proposal:v1");
                Evidence.Require((string)role["instance"]==instance&&(string)role["operator_sid"]==identity.User.Value,"Setup role differs from proposed principal/instance");
                string bundle=Path.GetDirectoryName(Native.LocalPath(proposalPath,true));
                var entries=new Dictionary<string,Dictionary<string,object>>(StringComparer.OrdinalIgnoreCase);
                var held=new List<SafeFileHandle>();
                try
                {
                    foreach(object item in (IEnumerable)value["files"])
                    {
                        var file=Evidence.Obj(item);Evidence.Closed(file,"relative_path","sha256","byte_length");
                        string relative=(string)file["relative_path"];
                        SetupStateValidator.FileAllowed(relative,instance);
                        Evidence.Require(!entries.ContainsKey(relative),"Duplicate setup file");
                        string path=Native.Child(Path.Combine(bundle,"depot"),relative);
                        held.AddRange(Native.Anchor(path));
                        var pinned=Native.Open(path,Native.ReadData|Native.ReadAttributes|Native.ReadControl,1);held.Add(pinned);
                        Evidence.Require(Native.HashFile(pinned)==(string)file["sha256"],"Setup file bytes changed");
                        Evidence.Require(new FileInfo(path).Length==Convert.ToInt64(file["byte_length"]),"Setup file size changed");entries.Add(relative,file);
                    }
                    foreach(string required in new string[]{"bin/PolicyOwner.exe","bin/ConditionalRuntimeProtectionHost.exe","worker-manifest.json","original-acquisitions.json","role-proposal.json","state/owner.sqlite3","state/lineage.sqlite3"})
                        Evidence.Require(entries.ContainsKey(required),"Setup bundle is incomplete: "+required);
                    Evidence.Require((string)entries["role-proposal.json"]["sha256"]==Native.Hash(OwnerWire.Bytes(role)),"Role bytes differ from setup proposal");
                    SetupStateValidator.Validate(Path.Combine(bundle,"depot"),instance,(string)value["ledger_identity"],entries);
                    using(var form=new Form())
                    using(var content=new TextBox())
                    using(var consent=new CheckBox())
                    using(var button=new Button())
                    {
                        form.Text="TradingAgents — Confirm "+(instance.StartsWith("test-",StringComparison.Ordinal)?"ISOLATED TEST":"OPERATIONAL")+" owner setup";
                        form.Width=1000;form.Height=780;form.StartPosition=FormStartPosition.CenterScreen;
                        content.Multiline=true;content.ReadOnly=true;content.ScrollBars=ScrollBars.Both;content.Dock=DockStyle.Fill;
                        content.Text="Proposed principal: "+(string)value["observed_account_name"]+"\r\nOperator SID: "+(string)value["operator_sid"]
                            +"\r\nInstance: "+instance+"\r\nNew service/account: TA.PolicyOwner."+instance+" / NT SERVICE\\TA.PolicyOwner."+instance
                            +"\r\nService SID: "+(string)value["service_sid"]+"\r\nNew root: "+root+"\r\nNew registration: HKLM\\"+registryPath
                            +"\r\nMechanism: Windows Hello WebAuthn, required human verification, dedicated virtual service account."
                            +"\r\nPermissions: Administrators/SYSTEM administer the new owner; operator reads its depot; the dedicated service writes protected authentication/artifact/ledger state."
                            +"\r\nThis creates the displayed new Windows service, HKLM registration and protected storage. It does not authenticate a role or approve any policy. Administrators and SYSTEM remain trusted. No existing object permissions are changed. Failed partial setup is retained and is never silently retried.\r\n\r\nExact complete setup proposal:\r\n"+Evidence.Canonical(value);
                        consent.Text="I confirm this exact principal, new storage, service registration and permissions.";consent.Dock=DockStyle.Bottom;consent.Height=45;
                        button.Text="Provision this new owner";button.Enabled=false;button.Dock=DockStyle.Bottom;button.Height=50;
                        consent.CheckedChanged+=delegate {button.Enabled=consent.Checked;};
                        button.Click+=delegate {form.DialogResult=DialogResult.OK;form.Close();};
                        form.Controls.Add(content);form.Controls.Add(consent);form.Controls.Add(button);
                        Evidence.Require(form.ShowDialog()==DialogResult.OK,"Setup was cancelled; no registration performed");
                    }
                    return Provision(value,registryPath,root,bundle,entries);
                }
                finally {foreach(var file in held)file.Dispose();}
            }
        }

        static Dictionary<string,object> Provision(Dictionary<string,object> proposal,string registryPath,string root,string bundle,Dictionary<string,Dictionary<string,object>> entries)
        {
            string instance=(string)proposal["instance"],principal=(string)proposal["operator_sid"],serviceSid=(string)proposal["service_sid"],serviceName="TA.PolicyOwner."+instance;
            IntPtr manager=OpenSCManagerW(null,null,3),service=IntPtr.Zero;
            if(manager==IntPtr.Zero)throw new Win32Exception(Marshal.GetLastWin32Error());
            using(var machine=RegistryKey.OpenBaseKey(RegistryHive.LocalMachine,RegistryView.Registry64))
            {
                try
                {
                    foreach(string parent in new string[]{@"SOFTWARE\TradingAgents",@"SOFTWARE\TradingAgents\PolicyOwner"})
                        using(var existing=machine.OpenSubKey(parent,RegistryKeyPermissionCheck.ReadSubTree,RegistryRights.ReadKey|RegistryRights.ReadPermissions))
                            if(existing!=null)OwnerAnchor.Descriptor(new RawSecurityDescriptor(existing.GetAccessControl(AccessControlSections.Owner|AccessControlSections.Access).GetSecurityDescriptorBinaryForm(),0),"",true);
                    using(var prior=machine.OpenSubKey(registryPath))Evidence.Require(prior==null,"Owner registry instance already exists");
                    IntPtr priorService=OpenServiceW(manager,serviceName,1);
                    if(priorService!=IntPtr.Zero) {CloseServiceHandle(priorService);throw new InvalidDataException("Owner service already exists");}
                    Evidence.Require(Marshal.GetLastWin32Error()==1060,"Unable to establish that service is absent");
                    using(var ancestors=new AncestorPins(root))
                    {
                        Evidence.Require(!Directory.Exists(root)&&!File.Exists(root),"Owner root appeared before provisioning");
                        byte[] initial=Native.FromSddl("O:BAG:BAD:P(A;OICI;FA;;;SY)(A;OICI;FA;;;BA)");
                        var pin=GCHandle.Alloc(initial,GCHandleType.Pinned);
                        try
                        {
                            var attributes=new CreateAttributes {Length=Marshal.SizeOf(typeof(CreateAttributes)),Descriptor=pin.AddrOfPinnedObject()};
                            if(!CreateDirectoryW(root,ref attributes))throw new Win32Exception(Marshal.GetLastWin32Error());
                        }
                        finally {pin.Free();}
                        foreach(var entry in entries)
                        {
                            string destination=Native.Child(root,entry.Key);Directory.CreateDirectory(Path.GetDirectoryName(destination));
                            using(var input=new FileStream(Native.Child(Path.Combine(bundle,"depot"),entry.Key),FileMode.Open,FileAccess.Read,FileShare.Read))
                            using(var output=new FileStream(destination,FileMode.CreateNew,FileAccess.Write,FileShare.None,65536,FileOptions.WriteThrough))
                            {input.CopyTo(output);output.Flush(true);}
                            Evidence.Require(Evidence.Canonical(Pin(destination))==Evidence.Canonical(new Dictionary<string,object>{{"sha256",entry.Value["sha256"]},{"byte_length",entry.Value["byte_length"]}}),"Copied setup bytes differ");
                        }
                        foreach(string directory in new string[]{"state/temp","authentication","artifacts"})Directory.CreateDirectory(Native.Child(root,directory));
                        string native=Path.Combine(root,"bin","PolicyOwner.exe");
                        service=CreateServiceW(manager,serviceName,"TradingAgents qualification owner "+instance,0x000f01ff,0x10,3,1,"\""+native+"\" service "+instance,null,IntPtr.Zero,null,@"NT SERVICE\"+serviceName,null);
                        if(service==IntPtr.Zero)throw new Win32Exception(Marshal.GetLastWin32Error());
                        string actual=((SecurityIdentifier)new NTAccount(@"NT SERVICE\"+serviceName).Translate(typeof(SecurityIdentifier))).Value;
                        Evidence.Require(actual==serviceSid,"Created service account differs from confirmed SID");
                        if(!SetServiceObjectSecurity(service,0x80000004,Native.FromSddl("D:P(A;;GA;;;SY)(A;;GA;;;BA)(A;;0x0002008D;;;"+principal+")")))throw new Win32Exception(Marshal.GetLastWin32Error());
                        foreach(string file in Directory.GetFiles(root,"*",SearchOption.AllDirectories))FileAcl(file,Dacl(serviceSid,principal,Mutable(file.Substring(root.Length+1).Replace('\\','/'))));
                        var directories=new List<string>(Directory.GetDirectories(root,"*",SearchOption.AllDirectories));directories.Sort(delegate(string left,string right){return right.Length.CompareTo(left.Length);});
                        foreach(string directory in directories)DirectoryAcl(directory,Dacl(serviceSid,principal,Mutable(directory.Substring(root.Length+1).Replace('\\','/'))));
                        DirectoryAcl(root,Dacl(serviceSid,principal,false));
                        byte[] rootDescriptor;using(var handle=Native.Open(root,Native.ReadAttributes|Native.ReadControl,3))rootDescriptor=Native.Descriptor(handle);
                        var storage=Evidence.Seal("policy-owner-storage-binding:v1",new Dictionary<string,object>{
                            {"contract_version","policy-owner-storage-binding-v1"},{"instance",instance},{"operational",instance.StartsWith("operational-",StringComparison.Ordinal)},
                            {"service_name",serviceName},{"service_sid",serviceSid},{"operator_sid",principal},{"owner_root",root},{"owner_root_object_id",Id(root)},
                            {"owner_root_descriptor_pin",new Dictionary<string,object>{{"sha256",Native.Hash(rootDescriptor)},{"byte_length",rootDescriptor.Length}}},
                            {"original_registry_pin",Pin(Path.Combine(root,"original-acquisitions.json"))},{"ledger_path",Path.Combine(root,"state","lineage.sqlite3")},
                            {"ledger_object_id",Id(Path.Combine(root,"state","lineage.sqlite3"))},{"owner_state_path",Path.Combine(root,"state","owner.sqlite3")},
                            {"owner_state_object_id",Id(Path.Combine(root,"state","owner.sqlite3"))},{"ledger_identity",proposal["ledger_identity"]},
                            {"native_owner_pin",Pin(native)},{"trusted_worker_manifest_pin",Pin(Path.Combine(root,"worker-manifest.json"))},
                            {"administrators_and_system_are_trusted",true},{"candidate_has_storage_write_access",false},{"registration_is_approval",false},{"execution_authorized",false}});
                        var bindings=new List<Dictionary<string,object>>();
                        foreach(string relative in new string[]{"state","state/temp","authentication","artifacts"})
                            using(var handle=Native.Open(Native.Child(root,relative),Native.ReadAttributes|Native.ReadControl,3))bindings.Add(new Dictionary<string,object>{{"relative_path",relative},{"object_id",Native.ObjectId(handle)},{"descriptor_sha256",Native.Hash(Native.Descriptor(handle))}});
                        var anchor=new Dictionary<string,object>{{"contract_version","windows-policy-owner-anchor-v1"},{"instance",instance},{"operational",storage["operational"]},
                            {"root",root},{"root_object_id",storage["owner_root_object_id"]},{"root_descriptor_sha256",Native.Hash(rootDescriptor)},
                            {"service_sid",serviceSid},{"operator_sid",principal},{"native_path",native},{"native_sha256",Pin(native)["sha256"]},
                            {"worker_manifest_sha256",Pin(Path.Combine(root,"worker-manifest.json"))["sha256"]},{"setup_proposal_sha256",Native.Hash(OwnerWire.Bytes(Evidence.Obj(proposal["role_proposal"])))},
                            {"original_registry_sha256",Pin(Path.Combine(root,"original-acquisitions.json"))["sha256"]},{"ledger_object_id",storage["ledger_object_id"]},
                            {"owner_state_object_id",storage["owner_state_object_id"]},{"protected_directories",bindings},{"ledger_identity",proposal["ledger_identity"]},{"storage_binding_identity",storage["identity"]}};
                        PolicyOwnerService.WriteImmutable(Path.Combine(root,"storage-binding.json"),OwnerWire.Bytes(storage),Path.Combine(root,"state","temp"));
                        var security=new RegistrySecurity();security.SetSecurityDescriptorSddlForm("O:BAG:BAD:P(A;CI;KA;;;SY)(A;CI;KA;;;BA)(A;CI;KR;;;"+serviceSid+")(A;CI;KR;;;"+principal+")");
                        foreach(string parent in new string[]{@"SOFTWARE\TradingAgents",@"SOFTWARE\TradingAgents\PolicyOwner"})
                            using(var existing=machine.OpenSubKey(parent))
                                if(existing==null)
                                {
                                    var namespaceSecurity=new RegistrySecurity();namespaceSecurity.SetSecurityDescriptorSddlForm("O:BAG:BAD:P(A;CI;KA;;;SY)(A;CI;KA;;;BA)(A;CI;KR;;;BU)(A;CI;KR;;;SU)");
                                    using(var created=machine.CreateSubKey(parent,RegistryKeyPermissionCheck.ReadWriteSubTree,RegistryOptions.None,namespaceSecurity)) { }
                                }
                        using(var key=machine.CreateSubKey(registryPath,RegistryKeyPermissionCheck.ReadWriteSubTree,RegistryOptions.None,security))
                        {Evidence.Require(key.GetValue("AnchorJson")==null,"Registration appeared during setup");key.SetValue("AnchorJson",Evidence.Canonical(anchor),RegistryValueKind.String);key.Flush();}
                        using(var verified=new OwnerAnchor(instance)) { }
                        if(!StartServiceW(service,0,IntPtr.Zero))throw new Win32Exception(Marshal.GetLastWin32Error());
                        return new Dictionary<string,object>{{"status","PROVISIONED_ROLE_NOT_ENROLLED"},{"instance",instance},{"storage_binding",storage},{"execution_authorized",false}};
                    }
                }
                finally {if(service!=IntPtr.Zero)CloseServiceHandle(service);CloseServiceHandle(manager);}
            }
        }
    }
}

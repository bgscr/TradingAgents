// The trust anchor is protected HKLM registration, never candidate JSON or an
// environment-selected executable. Windows administrators/SYSTEM are explicitly
// trusted; this is not a defense against an administrator rolling back a volume.
using System;
using System.Collections.Generic;
using System.ComponentModel;
using System.Diagnostics;
using System.IO;
using System.IO.Pipes;
using System.Runtime.InteropServices;
using System.Security.AccessControl;
using System.Security.Principal;
using System.Text;
using System.Text.RegularExpressions;
using System.Web.Script.Serialization;
using Microsoft.Win32;
using Microsoft.Win32.SafeHandles;
using TradingAgents.RuntimeProtection;

namespace TradingAgents.PolicyOwner
{
    internal sealed class OwnerAnchor : IDisposable
    {
        internal readonly Dictionary<string, object> Value;
        internal readonly List<SafeFileHandle> Held = new List<SafeFileHandle>();
        internal readonly string Instance, Root, ServiceSid, OperatorSid, NativePath, PipeName;
        internal readonly bool Operational;
        internal static string RegistryPath(string instance)
        {
            Evidence.Require(Regex.IsMatch(instance, "^(operational|test)-[0-9]{8}-[0-9]{3}$"), "Unknown owner instance syntax");
            return @"SOFTWARE\TradingAgents\PolicyOwner\" + instance;
        }
        internal static void Descriptor(RawSecurityDescriptor security, string serviceSid, bool registry)
        {
            Evidence.Require(security.Owner != null && (security.Owner.Value == "S-1-5-32-544" || security.Owner.Value == "S-1-5-18"
                || (!registry && security.Owner.Value == serviceSid)), "Owner storage has an untrusted object owner");
            Evidence.Require(security.DiscretionaryAcl != null, "Null owner DACL");
            int mutation = (registry ? 0x000d0026 : 0x000d0156) | 0x50000000;
            bool ownerRights = security.Owner.Value != serviceSid;
            foreach(GenericAce item in security.DiscretionaryAcl)
            {
                var ace = item as CommonAce;
                Evidence.Require(ace != null && !ace.IsCallback, "Unsupported owner ACL entry");
                if(ace.AceQualifier == AceQualifier.AccessDenied && ace.SecurityIdentifier.Value == "S-1-3-4"
                    && (ace.AccessMask & 0x000c0000) == 0x000c0000) ownerRights = true;
                if(ace.AceQualifier == AceQualifier.AccessAllowed && (ace.AccessMask & mutation) != 0)
                    Evidence.Require(ace.SecurityIdentifier.Value == "S-1-5-32-544" || ace.SecurityIdentifier.Value == "S-1-5-18"
                        || (!registry && ace.SecurityIdentifier.Value == serviceSid), "Candidate or unrelated principal can write owner storage");
            }
            Evidence.Require(ownerRights, "Service-owner implicit DACL rights are not suppressed");
        }
        internal OwnerAnchor(string instance)
        {
            Instance = instance;
            using(var machine = RegistryKey.OpenBaseKey(RegistryHive.LocalMachine, RegistryView.Registry64))
            {
              foreach(string parent in new string[]{@"SOFTWARE\TradingAgents",@"SOFTWARE\TradingAgents\PolicyOwner"})
                using(var parentKey=machine.OpenSubKey(parent,RegistryKeyPermissionCheck.ReadSubTree,RegistryRights.ReadKey|RegistryRights.ReadPermissions))
                {
                    Evidence.Require(parentKey!=null,"Owner registration namespace is missing");
                    Descriptor(new RawSecurityDescriptor(parentKey.GetAccessControl(AccessControlSections.Owner|AccessControlSections.Access).GetSecurityDescriptorBinaryForm(),0),"",true);
                }
              using(var key = machine.OpenSubKey(RegistryPath(instance), RegistryKeyPermissionCheck.ReadSubTree,
                RegistryRights.ReadKey | RegistryRights.ReadPermissions))
              {
                Evidence.Require(key != null, "Owner instance is not registered");
                Descriptor(new RawSecurityDescriptor(key.GetAccessControl(AccessControlSections.Owner | AccessControlSections.Access)
                    .GetSecurityDescriptorBinaryForm(), 0), "", true);
                string raw = key.GetValue("AnchorJson", null, RegistryValueOptions.DoNotExpandEnvironmentNames) as string;
                Evidence.Require(raw != null, "Owner registration is incomplete");
                Value = OwnerWire.Parse(Encoding.UTF8.GetBytes(raw));
              }
            }
            Evidence.Closed(Value, "contract_version", "instance", "operational", "root", "root_object_id", "root_descriptor_sha256",
                "service_sid", "operator_sid", "native_path", "native_sha256", "worker_manifest_sha256", "setup_proposal_sha256",
                "original_registry_sha256", "ledger_object_id", "owner_state_object_id", "protected_directories", "ledger_identity", "storage_binding_identity");
            Evidence.Require((string)Value["contract_version"] == "windows-policy-owner-anchor-v1" && (string)Value["instance"] == instance,
                "Owner anchor contract/instance mismatch");
            Operational = (bool)Value["operational"];
            Evidence.Require(Operational == instance.StartsWith("operational-", StringComparison.Ordinal), "Operational/test registration mismatch");
            Root = Native.LocalPath((string)Value["root"], false);
            ServiceSid = new SecurityIdentifier((string)Value["service_sid"]).Value;
            OperatorSid = new SecurityIdentifier((string)Value["operator_sid"]).Value;
            Evidence.Require(ServiceSid.StartsWith("S-1-5-80-", StringComparison.Ordinal) && ServiceSid != OperatorSid, "Separate service owner SID required");
            NativePath = Native.LocalPath((string)Value["native_path"], false);
            Evidence.Require(NativePath == Path.Combine(Root, "bin", "PolicyOwner.exe"), "Native owner outside registered depot");
            PipeName = "TA.PolicyOwner." + Instance;
            try
            {
                Held.AddRange(Native.Anchor(Root));
                var root = Native.Open(Root, Native.ReadAttributes | Native.ReadControl, 3); Held.Add(root);
                Evidence.Require(Native.ObjectId(root) == (string)Value["root_object_id"]
                    && Native.Hash(Native.Descriptor(root)) == (string)Value["root_descriptor_sha256"], "Registered owner root was replaced or permissions changed");
                Descriptor(new RawSecurityDescriptor(Native.Descriptor(root), 0), ServiceSid, false);
                var directories=new HashSet<string>(StringComparer.Ordinal);
                foreach(object item in (System.Collections.IEnumerable)Value["protected_directories"])
                {
                    var value=Evidence.Obj(item);Evidence.Closed(value,"relative_path","object_id","descriptor_sha256");
                    string relative=(string)value["relative_path"];
                    Evidence.Require(directories.Add(relative),"Duplicate registered owner directory");
                    var directory=Native.Open(Native.Child(Root,relative),Native.ReadAttributes|Native.ReadControl,3);Held.Add(directory);
                    Evidence.Require(Native.ObjectId(directory)==(string)value["object_id"]&&Native.Hash(Native.Descriptor(directory))==(string)value["descriptor_sha256"],
                        "Owner state directory was replaced or permissions changed");
                    Descriptor(new RawSecurityDescriptor(Native.Descriptor(directory),0),ServiceSid,false);
                }
                Evidence.Require(directories.SetEquals(new string[]{"state","state/temp","authentication","artifacts"}),"Owner directory bindings are incomplete");
                HoldFile(NativePath, (string)Value["native_sha256"]);
                HoldFile(Path.Combine(Root, "worker-manifest.json"), (string)Value["worker_manifest_sha256"]);
                HoldFile(Path.Combine(Root, "original-acquisitions.json"), (string)Value["original_registry_sha256"]);
                var ledger = Native.Open(Path.Combine(Root, "state", "lineage.sqlite3"), Native.ReadAttributes | Native.ReadControl, 3);
                Held.Add(ledger);
                Evidence.Require(Native.ObjectId(ledger) == (string)Value["ledger_object_id"], "Registered durable ledger was replaced");
                Descriptor(new RawSecurityDescriptor(Native.Descriptor(ledger), 0), ServiceSid, false);
                var state=Native.Open(Path.Combine(Root,"state","owner.sqlite3"),Native.ReadAttributes|Native.ReadControl,3);Held.Add(state);
                Evidence.Require(Native.ObjectId(state)==(string)Value["owner_state_object_id"],"Registered owner decision database was replaced");
                Descriptor(new RawSecurityDescriptor(Native.Descriptor(state),0),ServiceSid,false);
            }
            catch { Dispose(); throw; }
        }
        internal void HoldFile(string path, string sha256)
        {
            Evidence.Require(path.StartsWith(Root + "\\", StringComparison.Ordinal), "Owner depot path escape");
            Held.AddRange(Native.Anchor(path));
            var file = Native.Open(path, Native.ReadData | Native.ReadAttributes | Native.ReadControl, 1); Held.Add(file);
            Descriptor(new RawSecurityDescriptor(Native.Descriptor(file), 0), ServiceSid, false);
            Evidence.Require(Native.HashFile(file) == sha256, "Registered owner file bytes changed");
        }
        public void Dispose() { foreach(var handle in Held) handle.Dispose(); Held.Clear(); }
    }

    internal static class OwnerWire
    {
        const int MaximumBytes = 64 * 1024 * 1024;
        [DllImport("kernel32.dll", SetLastError=true)]
        static extern bool GetNamedPipeServerProcessId(SafePipeHandle pipe, out uint processId);
        [DllImport("kernel32.dll", SetLastError=true)]
        internal static extern bool GetNamedPipeClientProcessId(SafePipeHandle pipe, out uint processId);
        [DllImport("advapi32.dll", SetLastError=true)]
        static extern bool OpenProcessToken(IntPtr process, uint access, out SafeAccessTokenHandle token);
        [DllImport("kernel32.dll", SetLastError=true)]
        static extern SafeProcessHandle OpenProcess(uint access, bool inherit, uint pid);
        [DllImport("kernel32.dll", CharSet=CharSet.Unicode, SetLastError=true)]
        static extern bool QueryFullProcessImageNameW(SafeProcessHandle process, uint flags, StringBuilder name, ref uint count);
        [DllImport("advapi32.dll", SetLastError=true)]
        static extern bool GetTokenInformation(SafeAccessTokenHandle token, int kind, IntPtr data, uint length, out uint needed);
        [DllImport("advapi32.dll")]
        static extern IntPtr GetSidSubAuthorityCount(IntPtr sid);
        [DllImport("advapi32.dll")]
        static extern IntPtr GetSidSubAuthority(IntPtr sid, uint index);

        internal static uint IntegrityLevel(SafeAccessTokenHandle token)
        {
            uint needed;
            GetTokenInformation(token, 25, IntPtr.Zero, 0, out needed);
            Evidence.Require(needed > 0 && needed <= 4096, "Token integrity information unavailable");
            IntPtr data = Marshal.AllocHGlobal((int)needed);
            try
            {
                if(!GetTokenInformation(token, 25, data, needed, out needed)) throw new Win32Exception(Marshal.GetLastWin32Error());
                IntPtr sid = Marshal.ReadIntPtr(data); byte count = Marshal.ReadByte(GetSidSubAuthorityCount(sid));
                Evidence.Require(count > 0, "Invalid integrity SID");
                return unchecked((uint)Marshal.ReadInt32(GetSidSubAuthority(sid, (uint)count - 1)));
            }
            finally { Marshal.FreeHGlobal(data); }
        }

        internal static Dictionary<string, object> Parse(byte[] bytes)
        {
            Evidence.Require(bytes.Length > 0 && bytes.Length <= MaximumBytes, "Owner message size exceeds contract");
            string raw = new UTF8Encoding(false, true).GetString(bytes);
            var value = Evidence.Obj(new JavaScriptSerializer {MaxJsonLength=MaximumBytes, RecursionLimit=128}.DeserializeObject(raw));
            Evidence.Require(raw == Evidence.Canonical(value), "Owner JSON must be canonical without duplicate keys or trailing data");
            return value;
        }
        internal static byte[] Bytes(Dictionary<string, object> value)
        { return Encoding.UTF8.GetBytes(Evidence.Canonical(value)); }
        static byte[] ReadExact(Stream stream, int count)
        {
            byte[] bytes = new byte[count]; int offset = 0;
            while(offset < count)
            {
                int read = stream.Read(bytes, offset, count - offset);
                if(read == 0) throw new EndOfStreamException("Owner channel ended before its complete frame");
                offset += read;
            }
            return bytes;
        }
        internal static Dictionary<string, object> Read(Stream stream)
        {
            int length = BitConverter.ToInt32(ReadExact(stream, 4), 0);
            Evidence.Require(length > 0 && length <= MaximumBytes, "Owner frame size exceeds contract");
            return Parse(ReadExact(stream, length));
        }
        internal static void Write(Stream stream, Dictionary<string, object> value)
        {
            byte[] bytes = Bytes(value); Evidence.Require(bytes.Length <= MaximumBytes, "Owner frame is too large");
            byte[] length = BitConverter.GetBytes(bytes.Length);
            stream.Write(length, 0, length.Length); stream.Write(bytes, 0, bytes.Length); stream.Flush();
        }
        internal static void Acknowledge(Stream stream,Dictionary<string,object> request,Dictionary<string,object> response)
        { Write(stream,new Dictionary<string,object>{{"contract_version","windows-policy-owner-response-ack-v1"},{"nonce",request["nonce"]},{"response_sha256",Native.Hash(Bytes(response))}}); }
        internal static void ReadAcknowledgement(Stream stream,Dictionary<string,object> request,Dictionary<string,object> response)
        {
            var ack=Read(stream);Evidence.Closed(ack,"contract_version","nonce","response_sha256");
            Evidence.Require((string)ack["contract_version"]=="windows-policy-owner-response-ack-v1"&&Object.Equals(ack["nonce"],request["nonce"])
                &&(string)ack["response_sha256"]==Native.Hash(Bytes(response)),"Owner client did not acknowledge the complete response");
        }
        internal static string ProcessSid(uint pid, string requiredGroup, string expectedImage, bool? administratorsEnabled = null)
        {
            using(var process = OpenProcess(0x1000, false, pid))
            {
                if(process.IsInvalid) throw new Win32Exception(Marshal.GetLastWin32Error());
                if(expectedImage != null)
                {
                    var name = new StringBuilder(32768); uint count = (uint)name.Capacity;
                    if(!QueryFullProcessImageNameW(process, 0, name, ref count)) throw new Win32Exception(Marshal.GetLastWin32Error());
                    Evidence.Require(String.Equals(name.ToString(), expectedImage, StringComparison.OrdinalIgnoreCase), "Pipe owner image differs from registered native owner");
                }
                SafeAccessTokenHandle token;
                if(!OpenProcessToken(process.DangerousGetHandle(), 8, out token)) throw new Win32Exception(Marshal.GetLastWin32Error());
                using(token)
                using(var identity = new WindowsIdentity(token.DangerousGetHandle()))
                {
                    if(requiredGroup != null)
                        Evidence.Require(Native.TokenHasEnabledSid(token, requiredGroup), "Pipe process lacks the registered service SID");
                    if(administratorsEnabled.HasValue)
                    {
                        Evidence.Require(Native.TokenHasEnabledSid(token, "S-1-5-32-544") == administratorsEnabled.Value,
                            "Owner frontend/candidate token boundary mismatch");
                        if(administratorsEnabled.Value) Evidence.Require(IntegrityLevel(token) >= 0x3000, "Human frontend must have high integrity");
                    }
                    return identity.User.Value;
                }
            }
        }
        internal static Dictionary<string, object> Call(OwnerAnchor anchor, Dictionary<string, object> body)
        {
            byte[] random = new byte[32]; using(var generator = System.Security.Cryptography.RandomNumberGenerator.Create()) generator.GetBytes(random);
            string nonce = Convert.ToBase64String(random);
            var request = new Dictionary<string, object> {{"contract_version", "windows-policy-owner-rpc-v1"}, {"instance", anchor.Instance},
                {"nonce", nonce}, {"body", body}};
            using(var pipe = new NamedPipeClientStream(".", anchor.PipeName, PipeDirection.InOut, PipeOptions.Asynchronous, TokenImpersonationLevel.Identification))
            {
                pipe.Connect(10000);
                uint pid;
                if(!GetNamedPipeServerProcessId(pipe.SafePipeHandle, out pid)) throw new Win32Exception(Marshal.GetLastWin32Error());
                Evidence.Require(ProcessSid(pid, null, anchor.NativePath) == anchor.ServiceSid, "Owner does not run as its dedicated virtual service account");
                // Identity is checked before writing any request. The connected
                // pipe handle remains the channel through the complete response.
                var task = System.Threading.Tasks.Task.Run(delegate {
                    Write(pipe, request);var received=Read(pipe);Acknowledge(pipe,request,received);return received;
                });
                Evidence.Require(task.Wait(60000), "Owner request exceeded its bounded timeout");
                var response = task.Result;
                Evidence.Closed(response, "contract_version", "instance", "nonce", "request_sha256", "ok", "result", "error");
                Evidence.Require((string)response["contract_version"] == "windows-policy-owner-response-v1"
                    && (string)response["instance"] == anchor.Instance && (string)response["nonce"] == nonce
                    && (string)response["request_sha256"] == Native.Hash(Bytes(request)), "Owner response belongs to another exact request");
                Evidence.Require((bool)response["ok"], "Trusted owner rejected request: " + (string)response["error"]);
                return Evidence.Obj(response["result"]);
            }
        }
    }
}

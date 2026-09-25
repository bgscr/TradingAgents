// One project qualification owner. No provider, identity-provider, certificate,
// generic remote execution or live-analysis endpoint is exposed.
using System;
using System.Collections;
using System.Collections.Generic;
using System.ComponentModel;
using System.Diagnostics;
using System.Globalization;
using System.IO;
using System.IO.Pipes;
using System.Runtime.InteropServices;
using System.Security.AccessControl;
using System.Security.Principal;
using System.ServiceProcess;
using System.Text;
using System.Threading;
using System.Threading.Tasks;
using Microsoft.Win32.SafeHandles;
using TradingAgents.RuntimeProtection;

namespace TradingAgents.PolicyOwner
{
    internal sealed class PolicyOwnerService : ServiceBase
    {
        readonly string instance;
        volatile bool stopping;
        NamedPipeServerStream listening;
        readonly object workerLock=new object();
        ProtectedChildren activeWorker;
        readonly Dictionary<string, Challenge> pending = new Dictionary<string, Challenge>(StringComparer.Ordinal);
        DateTime lastClock = DateTime.MinValue;
        sealed class Challenge
        {
            internal string Purpose, Caller, Id;
            internal byte[] Bytes;
            internal Dictionary<string, object> Document;
            internal Dictionary<string, object> ChallengeDocument;
            internal DateTime Expires;
        }
        [StructLayout(LayoutKind.Sequential)]
        struct SecurityAttributes { internal int Length; internal IntPtr Descriptor; internal int Inherit; }
        [DllImport("kernel32.dll", CharSet=CharSet.Unicode, SetLastError=true)]
        static extern SafePipeHandle CreateNamedPipeW(string name, uint mode, uint pipeMode, uint instances,
            uint outputSize, uint inputSize, uint timeout, ref SecurityAttributes security);
        [DllImport("kernel32.dll", SetLastError=true)]
        static extern bool CancelIoEx(SafePipeHandle pipe, IntPtr overlapped);
        [DllImport("kernel32.dll",CharSet=CharSet.Unicode,SetLastError=true)]
        static extern bool MoveFileExW(string existing,string destination,uint flags);
        [DllImport("advapi32.dll", SetLastError=true)]
        static extern bool SetKernelObjectSecurity(IntPtr handle, uint information, byte[] descriptor);
        [DllImport("advapi32.dll", SetLastError=true)]
        static extern bool OpenProcessToken(IntPtr process, uint access, out SafeAccessTokenHandle token);
        [DllImport("advapi32.dll", SetLastError=true)]
        static extern bool SetTokenInformation(SafeAccessTokenHandle token, int kind, IntPtr value, int size);

        internal PolicyOwnerService(string name)
        { instance = name; ServiceName = "TA.PolicyOwner." + name; CanStop = true; AutoLog = false; }
        protected override void OnStart(string[] args)
        {
            Task.Run(delegate {
                try { Serve(); }
                catch(Exception error) { System.Diagnostics.Trace.WriteLine(error); Stop(); }
            });
        }
        protected override void OnStop()
        {
            stopping = true;
            lock(workerLock)if(activeWorker!=null)activeWorker.Terminate();
            if(listening != null) listening.Dispose();
        }

        // New owner process/token only. Grant the registered reader just enough
        // access to independently inspect the pipe server's image and token.
        internal static void ProtectProcess(OwnerAnchor anchor)
        {
            string grants = "(D;;WDWO;;;OW)(A;;GA;;;SY)(A;;GA;;;BA)(A;;GA;;;" + anchor.ServiceSid + ")";
            string process = "D:P" + grants + "(A;;0x00101000;;;" + anchor.OperatorSid + ")";
            string token = "D:P" + grants + "(A;;0x00020008;;;" + anchor.OperatorSid + ")";
            using(var current = Process.GetCurrentProcess())
            {
                SafeAccessTokenHandle access;
                if(!OpenProcessToken(current.Handle, 0x000e008b, out access)) throw new Win32Exception(Marshal.GetLastWin32Error());
                using(access)
                {
                    // Protect child process default DACLs before starting a worker.
                    var descriptor = new RawSecurityDescriptor(DefaultKernelDacl(anchor.ServiceSid));
                    byte[] acl = new byte[descriptor.DiscretionaryAcl.BinaryLength]; descriptor.DiscretionaryAcl.GetBinaryForm(acl, 0);
                    var pin = GCHandle.Alloc(acl, GCHandleType.Pinned);
                    IntPtr pointer = Marshal.AllocHGlobal(IntPtr.Size);
                    try
                    {
                        Marshal.WriteIntPtr(pointer, pin.AddrOfPinnedObject());
                        if(!SetTokenInformation(access, 6, pointer, IntPtr.Size)) throw new Win32Exception(Marshal.GetLastWin32Error()); // TokenDefaultDacl
                    }
                    finally { Marshal.FreeHGlobal(pointer); pin.Free(); }
                    if(!SetKernelObjectSecurity(access.DangerousGetHandle(), 0x80000004, Native.FromSddl(token)))
                        throw new Win32Exception(Marshal.GetLastWin32Error());
                }
                if(!SetKernelObjectSecurity(current.Handle, 0x80000004, Native.FromSddl(process)))
                    throw new Win32Exception(Marshal.GetLastWin32Error());
            }
        }
        internal static string DefaultKernelDacl(string serviceSid)
        {
            // A dedicated virtual account already separates the worker from
            // candidate users. The worker needs ordinary access to its own new
            // kernel objects during runtime startup. Storage ACLs separately
            // suppress implicit owner permission changes on durable evidence.
            return "D:P(A;;GA;;;SY)(A;;GA;;;BA)(A;;GA;;;"+serviceSid+")";
        }
        static NamedPipeServerStream Pipe(OwnerAnchor anchor)
        {
            byte[] descriptor = Native.FromSddl("D:P(D;;WDWO;;;OW)(A;;GA;;;" + anchor.ServiceSid
                + ")(A;;GA;;;SY)(A;;0x0012019F;;;" + anchor.OperatorSid + ")S:(ML;;NW;;;ME)");
            var pin = GCHandle.Alloc(descriptor, GCHandleType.Pinned);
            try
            {
                var security = new SecurityAttributes {Length=Marshal.SizeOf(typeof(SecurityAttributes)), Descriptor=pin.AddrOfPinnedObject()};
                // FIRST_PIPE_INSTANCE and REJECT_REMOTE_CLIENTS are native flags;
                // an existing squatter makes this owner fail closed.
                var handle = CreateNamedPipeW(@"\\.\pipe\" + anchor.PipeName, 0x40080003, 8, 1, 65536, 65536, 10000, ref security);
                if(handle.IsInvalid) { int error = Marshal.GetLastWin32Error(); handle.Dispose(); throw new Win32Exception(error); }
                return new NamedPipeServerStream(PipeDirection.InOut, true, false, handle);
            }
            finally { pin.Free(); }
        }
        DateTime Clock()
        {
            DateTime now = DateTime.UtcNow;
            Evidence.Require(now >= lastClock, "Owner clock regressed"); lastClock = now; return now;
        }
        static string Iso(DateTime value) { return value.ToString("yyyy-MM-ddTHH:mm:ss.fffffffZ", CultureInfo.InvariantCulture); }
        void Serve()
        {
            using(var anchor = new OwnerAnchor(instance))
            {
                using(var identity = WindowsIdentity.GetCurrent())
                    Evidence.Require(identity.User.Value == anchor.ServiceSid,
                        "Registered dedicated virtual service account required");
                ProtectProcess(anchor);
                // Pin every worker/interpreter/dependency byte for the service
                // lifetime. The candidate cannot replace Python before a check.
                var manifest = OwnerWire.Parse(File.ReadAllBytes(Path.Combine(anchor.Root, "worker-manifest.json")));
                Evidence.Closed(manifest, "contract_version", "files", "execution_authorized");
                Evidence.Require((string)manifest["contract_version"] == "policy-owner-worker-depot-v1" && !(bool)manifest["execution_authorized"], "Worker depot contract mismatch");
                var names = new HashSet<string>(StringComparer.OrdinalIgnoreCase);
                foreach(object item in (IEnumerable)manifest["files"])
                {
                    var file = Evidence.Obj(item); Evidence.Closed(file, "relative_path", "sha256", "byte_length");
                    string name = (string)file["relative_path"];
                    Evidence.Require(names.Add(name), "Duplicate worker depot file");
                    anchor.HoldFile(Native.Child(anchor.Root, name), (string)file["sha256"]);
                    Evidence.Require(new FileInfo(Native.Child(anchor.Root,name)).Length==Convert.ToInt64(file["byte_length"]),"Worker depot file length mismatch");
                }
                Evidence.Require(names.Contains("python/python.exe") && names.Contains("source/scripts/policy_owner/owner_worker.py"), "Missing trusted worker entrypoint/interpreter");
                foreach(string subtree in new string[]{"python","dependencies","source"})
                {
                    string depot=Path.Combine(anchor.Root,subtree);
                    var remaining=new Queue<string>();remaining.Enqueue(depot);
                    while(remaining.Count>0)
                    {
                        string directory=remaining.Dequeue();
                        var held=Native.Open(directory,Native.ReadAttributes|Native.ReadControl,3);anchor.Held.Add(held);
                        OwnerAnchor.Descriptor(new RawSecurityDescriptor(Native.Descriptor(held),0),anchor.ServiceSid,false);
                        foreach(string child in Directory.GetDirectories(directory))remaining.Enqueue(child);
                        foreach(string file in Directory.GetFiles(directory))
                            Evidence.Require(names.Contains(file.Substring(anchor.Root.Length+1).Replace('\\','/')),"Unregistered executable/import file in owner depot");
                    }
                }
                // Keep the FIRST_PIPE_INSTANCE handle for the entire owner
                // lifetime. Disconnecting a client never opens a squatting gap.
                using(var pipe=Pipe(anchor))
                {
                    listening=pipe;
                    try
                    {
                        while(!stopping)
                        {
                            Task connectionIo=null;
                            try
                            {
                                pipe.WaitForConnection();
                                var requestTask=Task.Run(delegate { return OwnerWire.Read(pipe); });
                                connectionIo=requestTask;
                                if(!requestTask.Wait(10000))throw new System.TimeoutException("Owner request framing timed out");
                                var request=requestTask.Result;
                                connectionIo=null;
                                var response=new Dictionary<string, object> {{"contract_version", "windows-policy-owner-response-v1"}, {"instance", instance},
                                    {"nonce", request.ContainsKey("nonce")?request["nonce"]:""}, {"request_sha256", Native.Hash(OwnerWire.Bytes(request))},
                                    {"ok", false}, {"result", null}, {"error", "Rejected"}};
                                try
                                {
                                    Evidence.Closed(request, "contract_version", "instance", "nonce", "body");
                                    Evidence.Require((string)request["contract_version"]=="windows-policy-owner-rpc-v1" && (string)request["instance"]==instance
                                        && Convert.FromBase64String((string)request["nonce"]).Length==32, "Owner RPC identity mismatch");
                                    string caller=null;bool admin=false;
                                    pipe.RunAsClient(delegate {
                                        using(var identity=WindowsIdentity.GetCurrent(true))
                                        {
                                            Evidence.Require(identity!=null,"Pipe authentication is unavailable");
                                            caller=identity.User.Value;admin=Native.TokenHasEnabledSid(identity.AccessToken,"S-1-5-32-544");
                                        }
                                    });
                                    Evidence.Require(caller==anchor.OperatorSid,"Unauthorized owner client principal");
                                    uint pid;
                                    if(!OwnerWire.GetNamedPipeClientProcessId(pipe.SafePipeHandle,out pid))throw new Win32Exception(Marshal.GetLastWin32Error());
                                    response["result"]=Dispatch(anchor,Evidence.Obj(request["body"]),caller,admin,pid);
                                    response["ok"]=true;response["error"]=null;
                                }
                                catch(Exception error) { response["error"]=error.GetType().Name+": "+error.Message; }
                                var write=Task.Run(delegate { OwnerWire.Write(pipe,response); });
                                connectionIo=write;
                                if(!write.Wait(10000))throw new System.TimeoutException("Owner response framing timed out");
                                // A pipe flush is not a reader acknowledgement.
                                // Keep this response until the client confirms it
                                // has read every byte, before Disconnect discards
                                // any remaining kernel pipe buffer.
                                var acknowledged=Task.Run(delegate {OwnerWire.ReadAcknowledgement(pipe,request,response);});
                                connectionIo=acknowledged;
                                if(!acknowledged.Wait(10000))throw new System.TimeoutException("Owner response acknowledgement timed out");
                            }
                            catch(Exception error)
                            {
                                if(stopping)break;
                                System.Diagnostics.Trace.WriteLine("Rejected owner connection: "+error.GetType().Name);
                            }
                            finally
                            {
                                if(!stopping)
                                {
                                    if(connectionIo!=null&&!connectionIo.IsCompleted)CancelIoEx(pipe.SafePipeHandle,IntPtr.Zero);
                                    if(pipe.IsConnected)pipe.Disconnect();
                                }
                                // Never reuse an instance with a read/write still
                                // attached to its previous client. A faulted task
                                // is complete; an undrained task stops the owner.
                                if(connectionIo!=null)
                                {
                                    try { connectionIo.Wait(5000); } catch(AggregateException) { }
                                    if(!connectionIo.IsCompleted)throw new InvalidOperationException("Owner pipe I/O did not quiesce");
                                }
                            }
                        }
                    }
                    finally { listening=null; }
                }
            }
        }
        Dictionary<string, object> Dispatch(OwnerAnchor anchor, Dictionary<string, object> request, string caller, bool admin, uint pid)
        {
            Evidence.Closed(request, "operation", "payload");
            string operation = (string)request["operation"];
            var payload = Evidence.Obj(request["payload"]);
            var records = ReadRecords(anchor);
            if(operation == "status")
            {
                Evidence.Closed(payload);
                return new Dictionary<string, object> {{"instance", instance}, {"operational", anchor.Operational}, {"enrolled", records.Count != 0},
                    {"storage_binding_identity", anchor.Value["storage_binding_identity"]}, {"execution_authorized", false}};
            }
            if(operation == "challenge" || operation == "complete-authentication")
            {
                // Windows Hello creation with NONE attestation is not a remote
                // attestation protocol. Enrollment is accepted only from the
                // protected elevated native frontend invoking the real API. The
                // candidate route is required to have a non-administrator token.
                Evidence.Require(admin && OwnerWire.ProcessSid(pid, null, anchor.NativePath, true) == caller,
                    "Human authentication requires the registered elevated native frontend");
                if(operation == "challenge")
                {
                    Evidence.Closed(payload, "purpose", "document");
                    string purpose = (string)payload["purpose"];
                    Evidence.Require(purpose == "ENROLL_PROJECT_ROLE" || purpose == "EXACT_POLICY_DECISION" || purpose == "REVOKE_DECISION"
                        || purpose == "REGISTER_EVIDENCE", "Unsupported project-owner decision purpose");
                    var document = Evidence.Obj(payload["document"]);
                    if(purpose == "ENROLL_PROJECT_ROLE")
                        Evidence.Require(records.Count == 0 && Native.Hash(OwnerWire.Bytes(document)) == (string)anchor.Value["setup_proposal_sha256"],
                            "Role enrollment differs from operator-confirmed setup or already exists");
                    else Evidence.Require(records.Count > 0, "No human project role is enrolled");
                    Worker(anchor, "validate-authentication-document", new Dictionary<string, object> {{"purpose", purpose}, {"document", document}}, records);
                    byte[] nonce = new byte[32]; using(var generator = System.Security.Cryptography.RandomNumberGenerator.Create()) generator.GetBytes(nonce);
                    DateTime now = Clock(); string id = Convert.ToBase64String(nonce);
                    var challengeDocument = new Dictionary<string, object> {{"contract_version", "policy-owner-human-challenge-v1"}, {"instance", instance},
                        {"storage_binding_identity", anchor.Value["storage_binding_identity"]}, {"operator_sid", caller}, {"purpose", purpose},
                        {"document_sha256", Native.Hash(OwnerWire.Bytes(document))}, {"nonce", id}, {"issued_utc", Iso(now)}, {"expires_utc", Iso(now.AddMinutes(10))}};
                    var obsolete = new List<string>(); foreach(var entry in pending) if(entry.Value.Expires <= now) obsolete.Add(entry.Key);
                    foreach(string key in obsolete) pending.Remove(key);
                    Evidence.Require(pending.Count < 4, "Too many pending human decisions");
                    var value = new Challenge {Id=id, Purpose=purpose, Caller=caller, Document=document, Expires=now.AddMinutes(10),
                        ChallengeDocument=challengeDocument, Bytes=WindowsPolicyAuthentication.Hash(OwnerWire.Bytes(challengeDocument))};
                    pending.Add(id, value);
                    var reply = new Dictionary<string, object> {{"challenge_id", id}, {"challenge", Convert.ToBase64String(value.Bytes)},
                        {"challenge_document", challengeDocument}, {"rp_id", anchor.Operational ? WindowsPolicyAuthentication.OperationalRp : WindowsPolicyAuthentication.TestRp},
                        {"credential_id", records.Count == 0 ? null : Evidence.Obj(records[0]["credential"])["id"]}};
                    return reply;
                }
                Evidence.Closed(payload, "challenge_id", "registration", "proof");
                string challengeId = (string)payload["challenge_id"]; Challenge challenge;
                Evidence.Require(pending.TryGetValue(challengeId, out challenge), "Missing, consumed, restarted or unknown human challenge");
                pending.Remove(challengeId); // Atomic on this single dispatch loop, before every acceptance/failure.
                Evidence.Require(challenge.Caller == caller && Clock() < challenge.Expires, "Wrong principal or expired human challenge");
                string rp = anchor.Operational ? WindowsPolicyAuthentication.OperationalRp : WindowsPolicyAuthentication.TestRp;
                Dictionary<string, object> credential;
                if(challenge.Purpose == "ENROLL_PROJECT_ROLE")
                {
                    Evidence.Require(records.Count == 0, "Project role enrollment was already consumed");
                    credential = Evidence.Obj(payload["registration"]); Evidence.Closed(credential, "id", "authenticator_data", "public_key");
                    byte[] key = WindowsPolicyAuthentication.RegistrationPublicKey(rp, Convert.FromBase64String((string)credential["authenticator_data"]),
                        Convert.FromBase64String((string)credential["id"]));
                    Evidence.Require(WindowsPolicyAuthentication.Equal(key, Convert.FromBase64String((string)credential["public_key"])), "Enrollment public key mismatch");
                }
                else
                {
                    Evidence.Require(payload["registration"] == null, "A decision cannot replace its enrolled credential");
                    credential = Evidence.Obj(records[0]["credential"]);
                }
                var proof = Proof(Evidence.Obj(payload["proof"]));
                uint previous = records.Count == 0 ? 0 : Convert.ToUInt32(records[records.Count - 1]["authenticator_counter"]);
                uint counter = WindowsPolicyAuthentication.Verify(rp, challenge.Bytes, Convert.FromBase64String((string)credential["id"]),
                    Convert.FromBase64String((string)credential["public_key"]), previous, proof);
                Worker(anchor, "validate-authentication-document", new Dictionary<string, object> {{"purpose", challenge.Purpose}, {"document", challenge.Document}}, records);
                Evidence.Require(Clock() < challenge.Expires, "Human challenge expired during complete decision validation");
                var record = Evidence.Seal("policy-owner-authenticated-human-record:v1", new Dictionary<string, object> {
                    {"contract_version", "policy-owner-authenticated-human-record-v1"}, {"instance", instance}, {"operator_sid", caller},
                    {"purpose", challenge.Purpose}, {"document", challenge.Document}, {"challenge_id", challenge.Id},
                    {"challenge", Convert.ToBase64String(challenge.Bytes)}, {"challenge_document", challenge.ChallengeDocument}, {"credential", credential}, {"proof", payload["proof"]},
                    {"authenticator_counter", counter}, {"recorded_at_utc", Iso(Clock())},
                    {"storage_binding_identity", anchor.Value["storage_binding_identity"]}, {"predecessor", records.Count == 0 ? null : records[records.Count - 1]["identity"]},
                    {"elevated_native_frontend_pin", anchor.Value["native_sha256"]}, {"execution_authorized", false}});
                var prospectiveRecords=new List<Dictionary<string,object>>(records);prospectiveRecords.Add(record);
                CheckAuthenticationCapacity(anchor.Value,record,prospectiveRecords,Iso(Clock()));
                Evidence.Require(Clock()<challenge.Expires,"Human challenge expired before immutable publication");
                WriteImmutable(Path.Combine(anchor.Root, "authentication", records.Count.ToString("D8", CultureInfo.InvariantCulture) + ".json"), OwnerWire.Bytes(record),Path.Combine(anchor.Root,"state","temp"));
                // Authentication is retained before any derived approval. A
                // crash cannot repeat its challenge or silently erase the proof.
                records.Add(record);
                return Worker(anchor, "apply-authenticated-record", record, records);
            }
            Evidence.Require(operation == "worker", "Unknown native owner operation");
            Evidence.Closed(payload, "operation", "payload");
            Evidence.Require((string)payload["operation"] != "apply-authenticated-record" && (string)payload["operation"] != "validate-authentication-document",
                "Native authentication dispatch is not an external worker operation");
            return Worker(anchor, (string)payload["operation"], Evidence.Obj(payload["payload"]), records);
        }
        internal static WindowsPolicyAuthentication.Proof Proof(Dictionary<string, object> value)
        {
            Evidence.Closed(value, "credential_id", "authenticator_data", "signature", "client_data");
            return new WindowsPolicyAuthentication.Proof {CredentialId=Convert.FromBase64String((string)value["credential_id"]),
                AuthenticatorData=Convert.FromBase64String((string)value["authenticator_data"]), Signature=Convert.FromBase64String((string)value["signature"]),
                ClientData=Convert.FromBase64String((string)value["client_data"])};
        }
        internal static void WriteImmutable(string path, byte[] bytes,string temporaryDirectory)
        {
            string temporary=Path.Combine(temporaryDirectory,"unpublished-"+Guid.NewGuid().ToString("N")+".tmp");
            using(var file = new FileStream(temporary, FileMode.CreateNew, FileAccess.Write, FileShare.None, 4096, FileOptions.WriteThrough))
            { file.Write(bytes, 0, bytes.Length); file.Flush(true); }
            // Same-volume, atomic and without REPLACE_EXISTING. Interrupted
            // writes remain in private temp, outside immutable record enumeration.
            if(!MoveFileExW(temporary,path,8))throw new Win32Exception(Marshal.GetLastWin32Error());
        }
        List<Dictionary<string, object>> ReadRecords(OwnerAnchor anchor)
        {
            var result = new List<Dictionary<string, object>>();
            string[] names = Directory.GetFiles(Path.Combine(anchor.Root, "authentication")); System.Array.Sort(names, StringComparer.Ordinal);
            var challenges = new HashSet<string>(StringComparer.Ordinal); uint counter = 0; Dictionary<string, object> enrolled = null;
            string rp = anchor.Operational ? WindowsPolicyAuthentication.OperationalRp : WindowsPolicyAuthentication.TestRp;
            foreach(string path in names)
            {
                Evidence.Require(Path.GetFileName(path) == result.Count.ToString("D8", CultureInfo.InvariantCulture) + ".json", "Human authentication record sequence changed");
                using(var file = new PinnedJson(path, null))
                {
                    var value = file.Value; Evidence.CheckIdentity(value, "policy-owner-authenticated-human-record:v1");
                    Evidence.Closed(value, "contract_version", "instance", "operator_sid", "purpose", "document", "challenge_id", "challenge", "challenge_document", "credential", "proof",
                        "authenticator_counter", "recorded_at_utc", "storage_binding_identity", "predecessor", "elevated_native_frontend_pin", "execution_authorized", "identity");
                    Evidence.Require((string)value["contract_version"] == "policy-owner-authenticated-human-record-v1" && (string)value["instance"] == instance
                        && (string)value["operator_sid"] == anchor.OperatorSid && (string)value["storage_binding_identity"] == (string)anchor.Value["storage_binding_identity"]
                        && !(bool)value["execution_authorized"] && challenges.Add((string)value["challenge_id"])
                        && Object.Equals(value["predecessor"], result.Count == 0 ? null : result[result.Count - 1]["identity"]), "Authenticated owner record binding/replay mismatch");
                    var challenge = Evidence.Obj(value["challenge_document"]);
                    Evidence.Closed(challenge, "contract_version", "instance", "storage_binding_identity", "operator_sid", "purpose", "document_sha256", "nonce", "issued_utc", "expires_utc");
                    Evidence.Require((string)challenge["contract_version"] == "policy-owner-human-challenge-v1" && (string)challenge["instance"] == instance
                        && Object.Equals(challenge["operator_sid"], value["operator_sid"]) && Object.Equals(challenge["purpose"], value["purpose"])
                        && Object.Equals(challenge["storage_binding_identity"], value["storage_binding_identity"]) && Object.Equals(challenge["nonce"], value["challenge_id"])
                        && (string)challenge["document_sha256"] == Native.Hash(OwnerWire.Bytes(Evidence.Obj(value["document"])))
                        && WindowsPolicyAuthentication.Equal(Convert.FromBase64String((string)value["challenge"]), WindowsPolicyAuthentication.Hash(OwnerWire.Bytes(challenge))),
                        "Human signature no longer binds the complete exact decision");
                    var credential = Evidence.Obj(value["credential"]);
                    Evidence.Closed(credential, "id", "authenticator_data", "public_key");
                    Evidence.Require((string)value["elevated_native_frontend_pin"] == (string)anchor.Value["native_sha256"], "Human record was enrolled through another native frontend");
                    if(result.Count == 0)
                    {
                        Evidence.Require((string)value["purpose"] == "ENROLL_PROJECT_ROLE"
                            && Native.Hash(OwnerWire.Bytes(Evidence.Obj(value["document"]))) == (string)anchor.Value["setup_proposal_sha256"], "Enrollment is not the confirmed exact setup");
                        enrolled = credential;
                        byte[] key = WindowsPolicyAuthentication.RegistrationPublicKey(rp, Convert.FromBase64String((string)credential["authenticator_data"]), Convert.FromBase64String((string)credential["id"]));
                        Evidence.Require(WindowsPolicyAuthentication.Equal(key, Convert.FromBase64String((string)credential["public_key"])), "Retained enrollment credential bytes disagree");
                    }
                    else Evidence.Require(Evidence.Canonical(enrolled) == Evidence.Canonical(credential) && (string)value["purpose"] != "ENROLL_PROJECT_ROLE", "Enrolled credential changed");
                    counter = WindowsPolicyAuthentication.Verify(rp, Convert.FromBase64String((string)value["challenge"]), Convert.FromBase64String((string)credential["id"]),
                        Convert.FromBase64String((string)credential["public_key"]), counter, Proof(Evidence.Obj(value["proof"])));
                    Evidence.Require(counter == Convert.ToUInt32(value["authenticator_counter"]), "Authentication counter changed");
                    DateTime recorded = DateTime.Parse((string)value["recorded_at_utc"], CultureInfo.InvariantCulture, DateTimeStyles.AdjustToUniversal | DateTimeStyles.AssumeUniversal);
                    DateTime issued = DateTime.Parse((string)challenge["issued_utc"], CultureInfo.InvariantCulture, DateTimeStyles.AdjustToUniversal | DateTimeStyles.AssumeUniversal);
                    DateTime expires = DateTime.Parse((string)challenge["expires_utc"], CultureInfo.InvariantCulture, DateTimeStyles.AdjustToUniversal | DateTimeStyles.AssumeUniversal);
                    Evidence.Require(issued <= recorded && recorded < expires && expires == issued.AddMinutes(10), "Human authentication was stale at acceptance");
                    Evidence.Require(recorded <= Clock(), "Authenticated decision is future dated");
                    result.Add(value);
                }
            }
            return result;
        }
        Dictionary<string, object> Worker(OwnerAnchor anchor, string operation, Dictionary<string, object> payload, List<Dictionary<string, object>> records)
        {
            var permitted = new HashSet<string>(new string[]{"validate-authentication-document", "apply-authenticated-record", "resolve", "read-artifact", "read-pin",
                "original-acquisition", "canonical-allocation", "observation", "executed-stage", "review-principal", "registered-root", "verify-envelope", "verify-attempt",
                "verify-materialization", "verify-inert-launch", "verify-result", "eligibility", "claim", "begin-stage", "complete-stage", "finalize", "issue-receipt", "report"}, StringComparer.Ordinal);
            Evidence.Require(permitted.Contains(operation), "No arbitrary worker or live execution operation exists");
            byte[] request=WorkerRequestBytes(anchor.Value,operation,payload,records,Iso(Clock()));
            var start = new ProcessStartInfo(Path.Combine(anchor.Root, "python", "python.exe")) {
                Arguments="-I -B -S \"" + Path.Combine(anchor.Root, "source", "scripts", "policy_owner", "owner_worker.py") + "\"",
                UseShellExecute=false, CreateNoWindow=true, RedirectStandardInput=true, RedirectStandardOutput=true, RedirectStandardError=true,
                WorkingDirectory=Path.Combine(anchor.Root, "state")};
            start.EnvironmentVariables.Clear();
            string windows = Environment.GetFolderPath(Environment.SpecialFolder.Windows);
            start.EnvironmentVariables["SystemRoot"] = windows;
            start.EnvironmentVariables["PATH"] = Path.Combine(anchor.Root, "python") + ";" + Path.Combine(windows, "System32");
            start.EnvironmentVariables["TEMP"] = Path.Combine(anchor.Root, "state", "temp");
            start.EnvironmentVariables["TMP"] = start.EnvironmentVariables["TEMP"];
            using(var children=new ProtectedChildren())
            {
                lock(workerLock)
                {
                    Evidence.Require(!stopping && activeWorker==null,"Owner worker lifetime is unavailable");
                    activeWorker=children;
                }
                try { return OwnerWire.Parse(children.RunRedirected(start,"\""+start.FileName+"\"",request,45000)); }
                finally { lock(workerLock)activeWorker=null; }
            }
        }
        internal static byte[] WorkerRequestBytes(Dictionary<string,object> anchor,string operation,Dictionary<string,object> payload,List<Dictionary<string,object>> records,string now)
        {
            var request=new Dictionary<string,object>{{"contract_version","policy-owner-trusted-worker-request-v1"},{"anchor",anchor},
                {"now_utc",now},{"operation",operation},{"payload",payload},{"authenticated_records",records}};
            byte[] bytes=OwnerWire.Bytes(request);
            Evidence.Require(bytes.Length<=64*1024*1024,"Owner worker request exceeds the complete serialized transport limit");
            return bytes;
        }
        internal static void CheckAuthenticationCapacity(Dictionary<string,object> anchor,Dictionary<string,object> record,List<Dictionary<string,object>> prospectiveRecords,string now)
        {
            // Reserve half of the worker channel for future policy/control
            // payloads. Capacity exhaustion rejects the new decision before
            // publication; it cannot erase prior proofs or renew any attempt.
            byte[] journal=System.Text.Encoding.UTF8.GetBytes(Evidence.Canonical(prospectiveRecords));
            Evidence.Require(journal.Length<=32*1024*1024,"Authenticated journal capacity exhausted before publication");
            WorkerRequestBytes(anchor,"apply-authenticated-record",record,prospectiveRecords,now);
        }
    }
}

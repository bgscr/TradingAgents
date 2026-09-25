// Inert local cryptographic tests. Ephemeral CNG keys are never enrolled as humans.
using System;
using System.Collections.Generic;
using System.IO;
using System.Diagnostics;
using System.Runtime.InteropServices;
using System.Security.AccessControl;
using System.Security.Principal;
using System.Security.Cryptography;
using System.Text;
using System.Web.Script.Serialization;
using TradingAgents.PolicyOwner;
using TradingAgents.RuntimeProtection;

internal static class PolicyAuthenticationContracts
{
    [DllImport("kernel32.dll", SetLastError=true)]
    static extern IntPtr OpenProcess(uint access, bool inherit, uint pid);
    [DllImport("kernel32.dll")]
    static extern bool CloseHandle(IntPtr handle);
    [DllImport("advapi32.dll", SetLastError=true)]
    static extern bool GetTokenInformation(Microsoft.Win32.SafeHandles.SafeAccessTokenHandle token, int kind, IntPtr value, uint size, out uint needed);
    [DllImport("advapi32.dll",SetLastError=true)]
    static extern bool OpenProcessToken(IntPtr process,uint access,out Microsoft.Win32.SafeHandles.SafeAccessTokenHandle token);
    [DllImport("advapi32.dll",SetLastError=true)]
    static extern bool SetTokenInformation(Microsoft.Win32.SafeHandles.SafeAccessTokenHandle token,int kind,IntPtr value,int length);
    static Dictionary<string, object> TokenObjectDefaults(Microsoft.Win32.SafeHandles.SafeAccessTokenHandle token)
    {
        var result = new Dictionary<string, object>();
        foreach(int kind in new int[]{4, 6})
        {
            uint needed; GetTokenInformation(token, kind, IntPtr.Zero, 0, out needed);
            if(needed == 0 || needed > 65536) throw new Exception("Token object defaults unavailable");
            IntPtr value = Marshal.AllocHGlobal((int)needed);
            try
            {
                if(!GetTokenInformation(token, kind, value, needed, out needed)) throw new System.ComponentModel.Win32Exception(Marshal.GetLastWin32Error());
                IntPtr pointer = Marshal.ReadIntPtr(value);
                if(kind == 4) result["owner"] = new SecurityIdentifier(pointer).Value;
                else if(pointer == IntPtr.Zero) result["default_dacl"] = null;
                else
                {
                    int length = (ushort)Marshal.ReadInt16(pointer, 2); byte[] raw = new byte[length]; Marshal.Copy(pointer, raw, 0, length);
                    var descriptor = new RawSecurityDescriptor(ControlFlags.DiscretionaryAclPresent, null, null, null, new RawAcl(raw, 0));
                    result["default_dacl"] = descriptor.GetSddlForm(AccessControlSections.Access);
                }
            }
            finally { Marshal.FreeHGlobal(value); }
        }
        return result;
    }
    static int passed;
    static void Reject(Action action)
    {
        try { action(); } catch(InvalidDataException) { passed++; return; }
        throw new Exception("Required rejection did not occur");
    }
    static byte[] Der(byte[] signature)
    {
        var result = new List<byte>();
        for(int index = 0; index < 2; index++)
        {
            int start = index * 32, end = start + 32;
            while(start < end - 1 && signature[start] == 0) start++;
            bool pad = (signature[start] & 0x80) != 0;
            result.Add(2); result.Add((byte)(end - start + (pad ? 1 : 0)));
            if(pad) result.Add(0);
            for(int i = start; i < end; i++) result.Add(signature[i]);
        }
        result.Insert(0, (byte)result.Count); result.Insert(0, 0x30); return result.ToArray();
    }
    static void Check()
    {
        string rp = WindowsPolicyAuthentication.TestRp;
        byte[] challenge = new byte[32], credential = new byte[32];
        using(var random = RandomNumberGenerator.Create()) { random.GetBytes(challenge); random.GetBytes(credential); }
        using(var signer = new ECDsaCng(256))
        {
            signer.HashAlgorithm = CngAlgorithm.Sha256;
            byte[] key = signer.Key.Export(CngKeyBlobFormat.EccPublicBlob);
            byte[] client = WindowsPolicyAuthentication.ClientJson(rp, challenge, false);
            byte[] auth = new byte[37];
            Array.Copy(WindowsPolicyAuthentication.Hash(Encoding.UTF8.GetBytes(rp)), auth, 32);
            auth[32] = 5; auth[36] = 1;
            byte[] signed = new byte[69]; Array.Copy(auth, signed, 37);
            Array.Copy(WindowsPolicyAuthentication.Hash(client), 0, signed, 37, 32);
            var proof = new WindowsPolicyAuthentication.Proof {CredentialId=credential, ClientData=client,
                AuthenticatorData=auth, Signature=Der(signer.SignData(signed))};
            if(WindowsPolicyAuthentication.Verify(rp, challenge, credential, key, 0, proof) != 1) throw new Exception("Counter");
            passed++;
            Reject(delegate { WindowsPolicyAuthentication.Verify(rp, challenge, new byte[32], key, 0, proof); });
            Reject(delegate { WindowsPolicyAuthentication.Verify(rp, new byte[32], credential, key, 0, proof); });
            Reject(delegate { WindowsPolicyAuthentication.Verify(WindowsPolicyAuthentication.OperationalRp, challenge, credential, key, 0, proof); });
            Reject(delegate { WindowsPolicyAuthentication.Verify(rp, challenge, credential, key, 1, proof); });
            using(var other = new ECDsaCng(256))
                Reject(delegate { WindowsPolicyAuthentication.Verify(rp, challenge, credential, other.Key.Export(CngKeyBlobFormat.EccPublicBlob), 0, proof); });
            auth[32] = 1;
            Reject(delegate { WindowsPolicyAuthentication.Verify(rp, challenge, credential, key, 0, proof); });
            auth[32] = 5; proof.Signature[proof.Signature.Length - 1] ^= 1;
            Reject(delegate { WindowsPolicyAuthentication.Verify(rp, challenge, credential, key, 0, proof); });
            Reject(delegate { WindowsPolicyAuthentication.DerSignature(new byte[]{0x30, 6, 2, 1, 0x80, 2, 1, 1}); });
            Reject(delegate { WindowsPolicyAuthentication.DerSignature(new byte[]{0x30, 7, 2, 2, 0, 1, 2, 1, 1}); });
            Reject(delegate { WindowsPolicyAuthentication.ClientJson("unregistered.local", challenge, false); });
            Reject(delegate { WindowsPolicyAuthentication.Create(IntPtr.Zero, rp, challenge, "S-1-0-0","test-20010101-001"); });
            Reject(delegate { WindowsPolicyAuthentication.Authenticate(IntPtr.Zero, rp, challenge, credential); });
            // Build an actual ES256 credential encoding and exercise the closed
            // registration parser independently of any platform enrollment.
            var attested = new List<byte>();
            attested.AddRange(auth); attested[32] = 0x45;
            attested.AddRange(new byte[16]); attested.Add(0); attested.Add((byte)credential.Length);
            attested.AddRange(credential);
            attested.AddRange(new byte[]{0xa5, 1, 2, 3, 0x26, 0x20, 1, 0x21, 0x58, 32});
            for(int i = 8; i < 40; i++) attested.Add(key[i]);
            attested.AddRange(new byte[]{0x22, 0x58, 32});
            for(int i = 40; i < 72; i++) attested.Add(key[i]);
            if(!WindowsPolicyAuthentication.Equal(WindowsPolicyAuthentication.RegistrationPublicKey(rp, attested.ToArray(), credential), key)) throw new Exception("COSE roundtrip");
            passed++;
            Reject(delegate { WindowsPolicyAuthentication.RegistrationPublicKey(rp, attested.ToArray(), new byte[32]); });
            attested.Add(0);
            Reject(delegate { WindowsPolicyAuthentication.RegistrationPublicKey(rp, attested.ToArray(), credential); });
            attested.RemoveAt(attested.Count - 1); attested[55 + credential.Length + 7] = 1;
            Reject(delegate { WindowsPolicyAuthentication.RegistrationPublicKey(rp, attested.ToArray(), credential); });
        }
    }
    static int RestrictedChild(string path, string parent)
    {
        Console.WriteLine("{\"label\":\"INERT_CHILD_ENTRY_REACHED\"}");
        using(var identity = WindowsIdentity.GetCurrent())
        {
            if(Native.TokenHasEnabledSid(identity.AccessToken, "S-1-5-32-544") || OwnerWire.IntegrityLevel(identity.AccessToken) != 0x2000) return 31;
        }
        bool denied = false;
        try { using(var file = new FileStream(path, FileMode.Open, FileAccess.Write)) { } }
        catch(UnauthorizedAccessException) { denied = true; }
        if(!denied) return 32;
        IntPtr process = OpenProcess(0x002a, false, UInt32.Parse(parent));
        if(process != IntPtr.Zero) { CloseHandle(process); return 33; }
        if(Marshal.GetLastWin32Error() != 5) return 34;
        Console.WriteLine("{\"label\":\"INERT_MEDIUM_WINDOWS_TOKEN\",\"administrator_enabled\":false,\"owner_file_write_denied\":true,\"owner_process_write_denied\":true,\"human_authenticated\":false,\"qualification_executed\":false}");
        return 0;
    }
    static void RestrictedContract(string directory)
    {
        string executable = System.Reflection.Assembly.GetExecutingAssembly().Location;
        directory = Native.LocalPath(Path.GetFullPath(directory), false);
        if(Path.GetDirectoryName(directory) != Path.GetDirectoryName(executable) || Directory.Exists(directory) || File.Exists(directory))
            throw new InvalidDataException("A fresh direct child of this inert executable's build directory is required");
        using(var root = Native.CreateNewDirectoryHandle(directory, true, "O:BAD:P(D;;WDWO;;;OW)(A;;FA;;;BA)(A;;FA;;;SY)(A;;FRFX;;;" + WindowsIdentity.GetCurrent().User.Value + ")")) { }
        using(var root = Native.Open(directory, 2 | Native.ReadAttributes | Native.ReadControl, 3))
            Console.WriteLine("{\"label\":\"INERT_OWNER_DIRECTORY_ACCESS\",\"add_file_access\":true,\"descriptor\":" + new JavaScriptSerializer().Serialize(new RawSecurityDescriptor(Native.Descriptor(root), 0).GetSddlForm(AccessControlSections.Owner | AccessControlSections.Access)) + "}");
        string probe = Path.Combine(directory, "owner-write-probe.bin");
        using(var handle = Native.CreateNewOwnerFileHandle(probe, "O:BAD:P(A;;FA;;;BA)(A;;FA;;;SY)"))
        using(var file = new FileStream(handle, FileAccess.ReadWrite))
        {
            file.WriteByte(73); file.Flush(true);
            Native.SetDacl(file.SafeFileHandle, Native.FromSddl("D:P(D;;WDWO;;;OW)(A;;FA;;;BA)(A;;FA;;;SY)(A;;FR;;;" + WindowsIdentity.GetCurrent().User.Value + ")"));
            if(new RawSecurityDescriptor(Native.Descriptor(file.SafeFileHandle), 0).Owner.Value != "S-1-5-32-544") throw new Exception("Fixture owner mismatch");
        }
        string childTemp = Path.Combine(directory, "candidate-temp");
        Native.CreateNewDirectory(childTemp, false);
        var info = new ProcessStartInfo(executable) {UseShellExecute=false, CreateNoWindow=true, WorkingDirectory=directory,
            Arguments="restricted-child \"" + probe + "\" " + Process.GetCurrentProcess().Id};
        info.EnvironmentVariables.Clear();
        info.EnvironmentVariables["SystemRoot"] = Environment.GetFolderPath(Environment.SpecialFolder.Windows);
        info.EnvironmentVariables["PATH"] = Path.Combine(info.EnvironmentVariables["SystemRoot"], "System32");
        info.EnvironmentVariables["TEMP"] = childTemp;
        info.EnvironmentVariables["TMP"] = childTemp;
        info.EnvironmentVariables["USERPROFILE"] = childTemp;
        info.EnvironmentVariables["LOCALAPPDATA"] = childTemp;
        info.EnvironmentVariables["APPDATA"] = childTemp;
        info.EnvironmentVariables["windir"] = info.EnvironmentVariables["SystemRoot"];
        using(var children = new ProtectedChildren())
        using(var child = children.Start(info, "\"" + executable + "\"", true, delegate { passed++; }))
        {
            if(!child.WaitForExit(15000) || children.ExitCode(child) != 0) throw new Exception("Restricted inert child failed: " + children.ExitCode(child));
            if(!children.Quiesce()) throw new Exception("Restricted descendants survived");
        }
        if(File.ReadAllBytes(probe).Length != 1 || File.ReadAllBytes(probe)[0] != 73) throw new Exception("Owner probe changed");
        // Verify rejection while suspended: this child would fail the parent
        // process assertion if it ran, but the callback must prevent all startup.
        using(var children = new ProtectedChildren())
        {
            bool rejected = false;
            try { using(var child = children.Start(info, "\"" + executable + "\"", true, delegate { throw new InvalidDataException("INERT_PRE_START_REJECTION"); })) { } }
            catch(InvalidDataException error) { rejected = error.Message == "INERT_PRE_START_REJECTION"; }
            if(!rejected || !children.Quiesce()) throw new Exception("Suspended child rejection was ineffective");
        }
        Console.WriteLine("{\"label\":\"INERT_NATIVE_CHILD_BOUNDARY\",\"restricted_child_passed\":true,\"pre_resume_rejection_passed\":true,\"owner_file_unchanged\":true,\"qualification_executed\":false}");
    }
    static void StartControl()
    {
        string executable = System.Reflection.Assembly.GetExecutingAssembly().Location;
        var info = new ProcessStartInfo(executable) {UseShellExecute=false, CreateNoWindow=true,
            WorkingDirectory=Path.GetDirectoryName(executable), Arguments="self-test"};
        info.EnvironmentVariables.Clear();
        info.EnvironmentVariables["SystemRoot"] = Environment.GetFolderPath(Environment.SpecialFolder.Windows);
        info.EnvironmentVariables["PATH"] = Path.Combine(info.EnvironmentVariables["SystemRoot"], "System32");
        using(var children = new ProtectedChildren())
        using(var child = children.Start(info, "\"" + executable + "\""))
        {
            if(!child.WaitForExit(15000)) throw new Exception("Inert control did not exit");
            Console.WriteLine("{\"label\":\"INERT_LEGACY_START_CONTROL\",\"exit_code\":" + children.ExitCode(child) + "}");
            if(children.ExitCode(child) != 0) throw new Exception("Legacy start control failed");
        }
    }
    static void WorkerTransport()
    {
        string executable=System.Reflection.Assembly.GetExecutingAssembly().Location;
        var info=new ProcessStartInfo(executable) {UseShellExecute=false,CreateNoWindow=true,WorkingDirectory=Path.GetDirectoryName(executable),Arguments="worker-echo"};
        info.EnvironmentVariables.Clear();
        info.EnvironmentVariables["SystemRoot"]=Environment.GetFolderPath(Environment.SpecialFolder.Windows);
        info.EnvironmentVariables["PATH"]=Path.Combine(info.EnvironmentVariables["SystemRoot"],"System32");
        byte[] request=Encoding.UTF8.GetBytes("{\"exact_bytes\":\"\u4e2d\u6587 \ud83d\udd12 "+new string('x',1024*1024)+"\"}");
        using(var children=new ProtectedChildren())
        {
            byte[] response=children.RunRedirected(info,"\""+executable+"\"",request,15000);
            if(!WindowsPolicyAuthentication.Equal(request,response))throw new Exception("Raw worker bytes changed");
        }
        info.Arguments="worker-block-input";var timer=Stopwatch.StartNew();bool rejected=false;
        using(var children=new ProtectedChildren())
        {
            try { children.RunRedirected(info,"\""+executable+"\"",request,1000); }
            catch(Exception error) { rejected=error is System.TimeoutException || error is AggregateException || error is IOException; }
            if(!rejected||!children.Quiesce()||timer.ElapsedMilliseconds>12000)throw new Exception("Blocked stdin escaped worker lifetime");
        }
        Console.WriteLine("{\"label\":\"INERT_PROTECTED_WORKER_TRANSPORT\",\"large_utf8_roundtrip\":true,\"blocked_stdin_terminated\":true,\"elapsed_ms\":"+timer.ElapsedMilliseconds+",\"human_authenticated\":false,\"qualification_executed\":false}");
    }
    static void AccountWorkerDefaults()
    {
        using(var identity=WindowsIdentity.GetCurrent())
        {
            var descriptor=new RawSecurityDescriptor(PolicyOwnerService.DefaultKernelDacl(identity.User.Value));
            byte[] acl=new byte[descriptor.DiscretionaryAcl.BinaryLength];descriptor.DiscretionaryAcl.GetBinaryForm(acl,0);
            var pin=GCHandle.Alloc(acl,GCHandleType.Pinned);IntPtr pointer=Marshal.AllocHGlobal(IntPtr.Size);
            try
            {
                Marshal.WriteIntPtr(pointer,pin.AddrOfPinnedObject());
                Microsoft.Win32.SafeHandles.SafeAccessTokenHandle token;
                if(!OpenProcessToken(new IntPtr(-1),0x80,out token))throw new System.ComponentModel.Win32Exception(Marshal.GetLastWin32Error());
                using(token)if(!SetTokenInformation(token,6,pointer,IntPtr.Size))throw new System.ComponentModel.Win32Exception(Marshal.GetLastWin32Error());
            }
            finally {Marshal.FreeHGlobal(pointer);pin.Free();}
        }
        // This fixture process is fresh and exits immediately afterward. Only
        // its own token defaults and newly created children are affected.
        WorkerTransport();
        Console.WriteLine("{\"label\":\"INERT_OWNER_WORKER_DEFAULT_DACL\",\"worker_transport_passed\":true,\"service_registered\":false}");
    }
    static void ResponseAcknowledgement()
    {
        string suffix=Guid.NewGuid().ToString("N");
        using(var server=RuntimeProtectionHost.OwnerPipe("inert:"+suffix,true))
        {
            for(int ordinal=0;ordinal<2;ordinal++)
            {
                var request=new Dictionary<string,object>{{"nonce",suffix+ordinal}};
                var response=new Dictionary<string,object>{{"retained_utf8","\u4e2d\u6587 "+new string('x',32768)}};
                var serving=System.Threading.Tasks.Task.Run(delegate {
                    server.WaitForConnection();OwnerWire.Write(server,response);
                    OwnerWire.ReadAcknowledgement(server,request,response);server.Disconnect();
                });
                using(var client=new System.IO.Pipes.NamedPipeClientStream(".","TradingAgentsProtection-"+suffix,System.IO.Pipes.PipeDirection.InOut))
                {
                    client.Connect(5000);System.Threading.Thread.Sleep(150);
                    var received=OwnerWire.Read(client);
                    if(Evidence.Canonical(received)!=Evidence.Canonical(response))throw new Exception("Delayed pipe reader lost bytes");
                    OwnerWire.Acknowledge(client,request,received);
                    if(!serving.Wait(5000))throw new Exception("Acknowledged server did not finish");
                }
            }
        }
        Console.WriteLine("{\"label\":\"INERT_OWNER_RESPONSE_ACK\",\"delayed_reader_exact_bytes\":true,\"sequential_instance_reuse\":true,\"service_registered\":false}");
    }
    static void GuardPipeContract()
    {
        string suffix=Guid.NewGuid().ToString("N"),executable=System.Reflection.Assembly.GetExecutingAssembly().Location;
        var info=new ProcessStartInfo(executable) {UseShellExecute=false,CreateNoWindow=true,WorkingDirectory=Path.GetDirectoryName(executable),Arguments="guard-pipe-child "+suffix};
        info.EnvironmentVariables.Clear();
        info.EnvironmentVariables["SystemRoot"]=Environment.GetFolderPath(Environment.SpecialFolder.Windows);
        info.EnvironmentVariables["PATH"]=Path.Combine(info.EnvironmentVariables["SystemRoot"],"System32");
        using(var pipe=RuntimeProtectionHost.OwnerPipe("inert:"+suffix,true))
        using(var children=new ProtectedChildren())
        using(var child=children.Start(info,"\""+executable+"\"",true))
        using(var deadline=new ChildDeadline(children,2000,delegate { pipe.Dispose(); }))
        {
            var exchange=System.Threading.Tasks.Task.Run(delegate {
                pipe.WaitForConnection();
                if(pipe.ReadByte()!=73)throw new Exception("Medium child could not write guard pipe");
                pipe.WriteByte(74);pipe.Flush();
            });
            if(!exchange.Wait(deadline.Remaining))throw new Exception("Guard pipe exchange timed out");
            if(!child.WaitForExit(12000)||!children.Quiesce())throw new Exception("Guard child survived deadline");
            bool expired=false;try { deadline.Check(); } catch(System.TimeoutException) { expired=true; }
            if(!expired||children.ExitCode(child)==0)throw new Exception("Guard deadline did not terminate waiting child");
        }
        Console.WriteLine("{\"label\":\"INERT_CONDITIONAL_GUARD_PIPE\",\"medium_child_write\":true,\"deadline_terminated_waiting_child\":true,\"qualification_executed\":false}");
    }
    public static int Main(string[] args)
    {
        try
        {
            Native.InitializePathRuntime();
            if(args.Length==1&&args[0]=="setup-client-file-admission")
            {
                foreach(string relative in new string[]{"test-client-dependencies/SYNTHETIC.py","bin/PolicyAuthenticationContracts.exe"})
                {
                    SetupStateValidator.FileAllowed(relative,"test-20300101-001");
                    foreach(string rejected in new string[]{"operational-20300101-001","test-invalid"})
                    {
                        bool denied=false;try {SetupStateValidator.FileAllowed(relative,rejected);}catch(InvalidOperationException){denied=true;}
                        if(!denied)throw new Exception("TEST client member escaped its namespace restriction");
                    }
                }
                foreach(string relative in new string[]{"authentication/00000000.json","state/imported-claim.json","bin/unreviewed.exe"})
                {
                    bool denied=false;try {SetupStateValidator.FileAllowed(relative,"test-20300101-001");}catch(InvalidOperationException){denied=true;}
                    if(!denied)throw new Exception("Unregistered setup state was admitted");
                }
                Console.WriteLine("{\"label\":\"INERT_TEST_SETUP_MEMBER_ADMISSION\",\"checks\":9,\"test_members_admitted\":true,\"operational_members_rejected\":true,\"provisioning_executed\":false}");
                return 0;
            }
            if(args.Length==1&&args[0]=="instance-user-handle")
            {
                string rp=WindowsPolicyAuthentication.TestRp,sid=WindowsIdentity.GetCurrent().User.Value;
                byte[] before=WindowsPolicyAuthentication.Hash(Encoding.UTF8.GetBytes(rp+"\n"+sid));
                byte[] first=WindowsPolicyAuthentication.RegistrationUserHandle(rp,sid,"test-20260914-001");
                byte[] second=WindowsPolicyAuthentication.RegistrationUserHandle(rp,sid,"test-20260915-002");
                if(first.Length!=32||second.Length!=32||WindowsPolicyAuthentication.Equal(before,second)||WindowsPolicyAuthentication.Equal(first,second)
                    ||!WindowsPolicyAuthentication.Equal(second,WindowsPolicyAuthentication.RegistrationUserHandle(rp,sid,"test-20260915-002")))throw new Exception("Enrollment handles are not isolated per instance");
                Reject(delegate {WindowsPolicyAuthentication.RegistrationUserHandle(rp,sid,"operational-20260915-002");});
                Console.WriteLine("{\"label\":\"INERT_INSTANCE_USER_HANDLE\",\"distinct_from_predecessor\":true,\"distinct_between_instances\":true,\"deterministic\":true,\"windows_hello_invoked\":false}");return 0;
            }
            if(args.Length==1&&args[0]=="authentication-capacity")
            {
                var anchor=new Dictionary<string,object>{{"fixture","ISOLATED TEST NO REGISTRATION"}};
                var small=new Dictionary<string,object>{{"test_data",new string('x',1024)}};
                PolicyOwnerService.CheckAuthenticationCapacity(anchor,small,new List<Dictionary<string,object>>{small},"2001-01-01T00:00:00.0000000Z");
                var large=new Dictionary<string,object>{{"test_data",new string('x',32*1024*1024)}};
                bool journalRejected=false;try {PolicyOwnerService.CheckAuthenticationCapacity(anchor,large,new List<Dictionary<string,object>>{large},"2001-01-01T00:00:00.0000000Z");}
                catch(InvalidOperationException error) {journalRejected=error.Message.Contains("journal capacity");}
                var almost=new Dictionary<string,object>{{"test_data",new string('x',32*1024*1024-64)}};
                bool aggregateRejected=false;try {PolicyOwnerService.CheckAuthenticationCapacity(anchor,almost,new List<Dictionary<string,object>>{almost},"2001-01-01T00:00:00.0000000Z");}
                catch(InvalidOperationException error) {aggregateRejected=error.Message.Contains("serialized transport limit");}
                if(!journalRejected||!aggregateRejected)throw new Exception("Authentication admission did not check complete future request capacity");
                Console.WriteLine("{\"label\":\"INERT_AUTHENTICATION_CAPACITY\",\"small_request_passed\":true,\"journal_overflow_rejected\":true,\"post_record_apply_overflow_rejected\":true,\"record_retained\":false}");return 0;
            }
            if(args.Length==2&&args[0]=="setup-state-check")
            {
                // Read-only validation through the same pre-provision boundary;
                // this fixture has no registration or human-authentication route.
                string proposalPath=Path.GetFullPath(args[1]);
                using(var proposal=new PinnedJson(proposalPath,Native.Hash(File.ReadAllBytes(proposalPath))))
                {
                    string depot=Path.Combine(Path.GetDirectoryName(proposalPath),"depot");
                    var entries=new Dictionary<string,Dictionary<string,object>>(StringComparer.OrdinalIgnoreCase);
                    var held=new List<Microsoft.Win32.SafeHandles.SafeFileHandle>();
                    try
                    {
                        foreach(object item in (System.Collections.IEnumerable)proposal.Value["files"])
                        {
                            var entry=Evidence.Obj(item);string relative=(string)entry["relative_path"];
                            SetupStateValidator.FileAllowed(relative,(string)proposal.Value["instance"]);entries.Add(relative,entry);
                            string path=Native.Child(depot,relative);
                            held.AddRange(Native.Anchor(path));
                            var file=Native.Open(path,Native.ReadData|Native.ReadAttributes|Native.ReadControl,1);held.Add(file);
                            Evidence.Require(Native.HashFile(file)==(string)entry["sha256"]&&new FileInfo(path).Length==Convert.ToInt64(entry["byte_length"]),"Fixture setup file changed");
                        }
                        SetupStateValidator.Validate(depot,(string)proposal.Value["instance"],(string)proposal.Value["ledger_identity"],entries);
                    }
                    finally {foreach(var file in held)file.Dispose();}
                }
                Console.WriteLine("{\"label\":\"INERT_NATIVE_SETUP_STATE\",\"pristine\":true,\"service_registered\":false}");return 0;
            }
            if(args.Length==1&&args[0]=="owner-worker-defaults") {AccountWorkerDefaults();return 0;}
            if(args.Length==1&&args[0]=="response-acknowledgement") {ResponseAcknowledgement();return 0;}
            if(args.Length==2&&args[0]=="guard-pipe-child")
            {
                using(var identity=WindowsIdentity.GetCurrent())
                    if(OwnerWire.IntegrityLevel(identity.AccessToken)!=0x2000||Native.TokenHasEnabledSid(identity.AccessToken,"S-1-5-32-544"))throw new Exception("Guard fixture was not reduced");
                using(var pipe=new System.IO.Pipes.NamedPipeClientStream(".","TradingAgentsProtection-"+args[1],System.IO.Pipes.PipeDirection.InOut))
                { pipe.Connect(5000);pipe.WriteByte(73);pipe.Flush();if(pipe.ReadByte()!=74)throw new Exception("Guard reply mismatch");System.Threading.Thread.Sleep(60000); }
                return 0;
            }
            if(args.Length==1&&args[0]=="guard-pipe-contract") { GuardPipeContract();return 0; }
            if(args.Length==1&&args[0]=="worker-block-input") { System.Threading.Thread.Sleep(60000);return 0; }
            if(args.Length==1&&args[0]=="worker-echo") { Console.OpenStandardInput().CopyTo(Console.OpenStandardOutput());return 0; }
            if(args.Length==1&&args[0]=="worker-transport") { WorkerTransport();return 0; }
            if(args.Length == 1 && args[0] == "start-control") { StartControl(); return 0; }
            if(args.Length == 1 && args[0] == "token-defaults")
            {
                using(var identity = WindowsIdentity.GetCurrent())
                using(var child = ProtectedChildren.ConditionalChildToken())
                    Console.WriteLine(new JavaScriptSerializer().Serialize(new Dictionary<string, object> {
                        {"label", "INERT_TOKEN_DEFAULT_OBJECT_PERMISSIONS"}, {"current", TokenObjectDefaults(identity.AccessToken)},
                        {"new_conditional", TokenObjectDefaults(child)}, {"human_authenticated", false}, {"child_launched", false}}));
                return 0;
            }
            if(args.Length == 1 && args[0] == "token-probe")
            {
                using(var identity = WindowsIdentity.GetCurrent()) Console.WriteLine(new JavaScriptSerializer().Serialize(new Dictionary<string, object> {
                    {"label", "READ_ONLY_CURRENT_PROCESS_TOKEN"}, {"observed_user_sid", identity.User.Value},
                    {"enabled_administrators", Native.TokenHasEnabledSid(identity.AccessToken, "S-1-5-32-544")},
                    {"integrity_level", OwnerWire.IntegrityLevel(identity.AccessToken)},
                    {"restricting_sids", Native.TokenRestrictingSids(identity.AccessToken)},
                    {"human_authenticated", false}, {"principal_enrolled", false}}));
                return 0;
            }
            if(args.Length == 3 && args[0] == "restricted-child") return RestrictedChild(args[1], args[2]);
            if(args.Length == 2 && args[0] == "restricted-contract") { RestrictedContract(args[1]); return 0; }
            if(args.Length != 1) throw new InvalidDataException("One explicit inert operation required");
            if(args[0] == "probe") Console.WriteLine(new JavaScriptSerializer().Serialize(WindowsPolicyAuthentication.Probe()));
            else if(args[0] == "self-test")
            {
                Check();
                Console.WriteLine(new JavaScriptSerializer().Serialize(new Dictionary<string, object> {
                    {"label", "INERT_EPHEMERAL_CNG_CRYPTOGRAPHIC_TESTS"}, {"checks", passed},
                    {"human_authenticated", false}, {"principal_enrolled", false},
                    {"qualification_claimed", false}, {"execution_authorized", false}}));
            }
            else throw new InvalidDataException("No enrollment or approval operation exists in this test executable");
            return 0;
        }
        catch(Exception error) { Console.Error.WriteLine(error); return 2; }
    }
}

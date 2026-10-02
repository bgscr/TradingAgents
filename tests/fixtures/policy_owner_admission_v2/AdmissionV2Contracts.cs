// TEST-only assembly: fake RPC and WebAuthn boundary; CNG verifies public test
// material in memory. No service/registry/ACL/Hello/installed-state entry runs.
using System;
using System.Collections.Generic;
using System.IO;
using System.Text;
using TradingAgents.PolicyOwner;
using TradingAgents.RuntimeProtection;

public static class AdmissionV2Contracts
{
    static int count;
    static void Check(bool condition,string label)
    {if(!condition)throw new InvalidDataException("TEST FAILURE: "+label);count++;}
    static void Reject(Action action,string label)
    {bool rejected=false;try{action();}catch(InvalidDataException){rejected=true;}Check(rejected,label);}
    sealed class FakeRpc : IAdmissionRpc
    {
        internal readonly Dictionary<string,object> Vector,Reserve;
        internal readonly List<string> Calls=new List<string>();
        internal bool FailIntent,FailBoundary;
        internal Dictionary<string,object> Boundary;
        internal FakeRpc(Dictionary<string,object> vector){Vector=vector;Reserve=Evidence.Obj(vector["challenge"]);}
        public Dictionary<string,object> Call(string operation,Dictionary<string,object> payload)
        {
            Calls.Add(operation);
            if(operation=="verify-complete-v2")
            {Check(Calls.Count==4&&payload.Count==0,"complete closure verified after acceptance");return new Dictionary<string,object>{{"status","CONTEXT_ESTABLISHED_STOP"},{"execution_authorized",false}};}
            Check(Convert.ToInt64(payload["expected_epoch"])==Convert.ToInt64(Reserve["epoch"])&&Object.Equals(payload["attempt"],Reserve["attempt_id"]),"RPC epoch/attempt binding");
            if(operation=="begin-creation-v2")
            {
                Check(Calls.Count==1,"creation intent first");if(FailIntent)throw new InvalidDataException("EXPECTED_INTENT_FAILURE");
                return new Dictionary<string,object>{{"status","CREATION_BEGUN"},{"attempt",Reserve["attempt_id"]},{"epoch",Reserve["epoch"]},
                    {"creation_receipt",Vector["creation_receipt"]},{"user_handle",Reserve["user_handle"]},{"execution_authorized",false}};
            }
            if(operation=="creation-boundary-v2")
            {
                Check(Calls.Count==2,"boundary follows durable intent");if(FailBoundary)throw new InvalidDataException("EXPECTED_BOUNDARY_FAILURE");
                Check(Object.Equals(payload["creation_receipt"],Vector["creation_receipt"]),"creation receipt binding");Boundary=Evidence.Obj(payload["parameters"]);
                return new Dictionary<string,object>{{"status","BOUNDARY_RECORDED"},{"creation_receipt",payload["creation_receipt"]},{"execution_authorized",false}};
            }
            Check(operation=="accept-v2"&&Calls.Count==3&&Boundary!=null,"only accept after boundary");
            Check(Evidence.Canonical(payload["registration"])==Evidence.Canonical(Vector["registration"])&&Evidence.Canonical(payload["proof"])==Evidence.Canonical(Vector["proof"]),"exact raw registration/proof transport");
            return new Dictionary<string,object>{{"status","ENROLLMENT_V2_COMPLETE_STOP"},{"phase","COMPLETE_STOP"},{"execution_authorized",false}};
        }
    }
    sealed class FakeAuthenticator : IAdmissionAuthenticator
    {
        readonly Dictionary<string,object> vector;internal int Creates,Assertions;
        internal FakeAuthenticator(Dictionary<string,object> value){vector=value;}
        public WindowsPolicyAuthentication.Registration Create(IntPtr window,string rp,byte[] challenge,string sid,byte[] handle,Action<Dictionary<string,object>> boundary)
        {
            Creates++;bool stopped=false;
            WindowsPolicyAuthentication.TestMakeBoundary=delegate(Dictionary<string,object> actual){boundary(actual);};
            try{WindowsPolicyAuthentication.TestCreateAtBoundary(window,rp,challenge,sid,handle);}
            catch(InvalidOperationException error){if(error.Message!="EXPECTED_TEST_BOUNDARY_STOP")throw;stopped=true;}
            finally{WindowsPolicyAuthentication.TestMakeBoundary=null;}
            Check(stopped,"native API intercepted at actual marshalled structs");var registration=Evidence.Obj(vector["registration"]);
            return new WindowsPolicyAuthentication.Registration{CredentialId=Convert.FromBase64String((string)registration["id"]),
                AuthenticatorData=Convert.FromBase64String((string)registration["authenticator_data"]),PublicKey=Convert.FromBase64String((string)registration["public_key"])};
        }
        public WindowsPolicyAuthentication.Proof Authenticate(IntPtr window,string rp,byte[] challenge,byte[] id)
        {Assertions++;return Proof(Evidence.Obj(vector["proof"]));}
    }
    static WindowsPolicyAuthentication.Proof Proof(Dictionary<string,object> value)
    {return new WindowsPolicyAuthentication.Proof{CredentialId=Convert.FromBase64String((string)value["credential_id"]),
        AuthenticatorData=Convert.FromBase64String((string)value["authenticator_data"]),Signature=Convert.FromBase64String((string)value["signature"]),ClientData=Convert.FromBase64String((string)value["client_data"])};}
    public static int Main(string[] args)
    {
        try
        {
            Check(args.Length==4&&Path.IsPathRooted(args[0])&&Path.IsPathRooted(args[2]),"exact synthetic vector and formal metadata image pins required");
            byte[] bytes=File.ReadAllBytes(args[0]);Check(Native.Hash(bytes)==args[1],"new core vector pin");var vector=OwnerWire.Parse(bytes);
            Check((string)vector["context"]=="SYNTHETIC_TEST_ONLY","synthetic vector context");
            var assembly=System.Reflection.Assembly.GetExecutingAssembly();
            foreach(string name in new[]{"TradingAgents.PolicyOwner.PolicyOwnerService","TradingAgents.PolicyOwner.PolicyOwnerSetup",
                "TradingAgents.PolicyOwner.EvidenceTransfer","TradingAgents.PolicyOwner.NativeConditionalOwner"})
                Check(assembly.GetType(name,false)==null,"legacy type absent from compiled v2 assembly: "+name);
            Check(typeof(RuntimeProtectionHost).GetMethod("Run",System.Reflection.BindingFlags.Static|System.Reflection.BindingFlags.Public|System.Reflection.BindingFlags.NonPublic)==null,
                "runtime Run implementation absent from compiled v2 assembly");
            Check(typeof(OwnerWire).GetMethod("Call",System.Reflection.BindingFlags.Static|System.Reflection.BindingFlags.Public|System.Reflection.BindingFlags.NonPublic)==null,
                "legacy generic RPC client absent from compiled v2 assembly");
            Check(Native.Hash(File.ReadAllBytes(args[2]))==args[3],"formal binary metadata input pin");
            var formal=System.Reflection.Assembly.ReflectionOnlyLoadFrom(args[2]);
            foreach(string name in new[]{"TradingAgents.PolicyOwner.PolicyOwnerService","TradingAgents.PolicyOwner.PolicyOwnerSetup",
                "TradingAgents.PolicyOwner.EvidenceTransfer","TradingAgents.PolicyOwner.NativeConditionalOwner","AdmissionV2Contracts"})
                Check(formal.GetType(name,false)==null,"formal binary excludes legacy/TEST type: "+name);
            Check(formal.GetType("TradingAgents.PolicyOwner.WindowsPolicyAuthentication",true).GetField("TestMakeBoundary",System.Reflection.BindingFlags.Static|System.Reflection.BindingFlags.NonPublic)==null,
                "formal binary contains no test authenticator hook");
            foreach(string operation in new[]{"setup","service","probe","evidence-capabilities","request","status","enroll","decide","revoke","register-evidence","transfer-worker-v2","initialize","prepare","inspect","run","unknown"})
                Check(PolicyOwnerMain.Main(new[]{operation,"operational-20990101-001"})==2,"legacy entry denied: "+operation);
            Check(PolicyOwnerMain.Main(new[]{"service-v2","operational-20990101-001"})==2,"TEST no formal service fallback");
            Check(PolicyOwnerMain.Main(new[]{"enroll-v2","operational-20990101-001"})==2,"TEST no formal frontend fallback");
            Check(ConditionalRuntimeProtectionHost.Main(new[]{"run-inert","unused","unused","unused"})==2,"conditional execution denied before anchor");
            Check(RuntimeProtectionHost.Main(new[]{"run","unused","unused"})==2,"standalone execution denied before input");
            foreach(string operation in new[]{"migrate-closed-v2","adopt-closed-v2","worker","challenge","complete-authentication","evidence-chunk-v2","apply-authenticated-record"})
                Check(!AdmissionV2.RpcOperation(operation),"public RPC bypass denied: "+operation);
            var reserve=Evidence.Obj(vector["challenge"]);var rpc=new FakeRpc(vector);var auth=new FakeAuthenticator(vector);
            var complete=AdmissionV2.Complete(rpc,auth,new IntPtr(1),(string)vector["operator_sid"],reserve);
            Check((string)complete["status"]=="CONTEXT_ESTABLISHED_STOP"&&auth.Creates==1&&auth.Assertions==1,"one creation/one proof then independent closure, no retry");
            Check((string)rpc.Boundary["user_id"]==(string)vector["expected_user_handle"]&&Convert.ToInt32(rpc.Boundary["user_size"])==32,"actual user ID/length");
            Check(Convert.ToInt32(rpc.Boundary["attachment"])==1&&Convert.ToInt32(rpc.Boundary["verification"])==1&&Convert.ToInt32(rpc.Boundary["attestation"])==1
                &&Convert.ToInt32(rpc.Boundary["algorithm"])==-7&&(bool)rpc.Boundary["window_nonzero"]&&(string)rpc.Boundary["boundary"]=="MARSHALLED_BEFORE_CALL","actual platform/UV/NONE/ES256 parameters");
            Check((string)rpc.Boundary["client_data"]==Convert.ToBase64String(WindowsPolicyAuthentication.ClientJson((string)reserve["rp_id"],Convert.FromBase64String((string)reserve["challenge"]),true)),"actual create clientData");
            var failedRpc=new FakeRpc(vector){FailIntent=true};var failedAuth=new FakeAuthenticator(vector);
            Reject(delegate{AdmissionV2.Complete(failedRpc,failedAuth,new IntPtr(1),(string)vector["operator_sid"],reserve);},"intent failure stops before credential API");
            Check(failedAuth.Creates==0&&failedAuth.Assertions==0&&failedRpc.Calls.Count==1,"no create on lost intent");
            var boundaryRpc=new FakeRpc(vector){FailBoundary=true};var boundaryAuth=new FakeAuthenticator(vector);
            Reject(delegate{AdmissionV2.Complete(boundaryRpc,boundaryAuth,new IntPtr(1),(string)vector["operator_sid"],reserve);},"boundary persistence failure stops native call");
            Check(boundaryAuth.Assertions==0&&boundaryRpc.Calls.Count==2,"no assertion/accept/retry on lost boundary receipt");
            var registration=Evidence.Obj(vector["registration"]);byte[] id=Convert.FromBase64String((string)registration["id"]),key=Convert.FromBase64String((string)registration["public_key"]),challenge=Convert.FromBase64String((string)reserve["challenge"]);
            var proof=Proof(Evidence.Obj(vector["proof"]));string rp=(string)reserve["rp_id"];
            Check(WindowsPolicyAuthentication.Equal(WindowsPolicyAuthentication.RegistrationPublicKey(rp,Convert.FromBase64String((string)registration["authenticator_data"]),id),key),"independent native COSE/public key parser");
            Check(WindowsPolicyAuthentication.Verify(rp,challenge,id,key,0,proof)==1,"independent CNG verification of Python synthetic ES256 proof");
            Reject(delegate{WindowsPolicyAuthentication.Verify(rp,challenge,id,key,1,proof);},"counter replay");
            byte[] altered=(byte[])challenge.Clone();altered[0]^=1;
            Reject(delegate{WindowsPolicyAuthentication.Verify(rp,altered,id,key,0,proof);},"challenge substitution");
            var bad=Proof(Evidence.Obj(vector["proof"]));bad.Signature=new byte[]{0x30,6,2,1,0,2,1,0};
            Reject(delegate{WindowsPolicyAuthentication.Verify(rp,challenge,id,key,0,bad);},"zero r/s");
            bad=Proof(Evidence.Obj(vector["proof"]));bad.Signature=new byte[]{0x30,7,2,2,0,1,2,1,1};
            Reject(delegate{WindowsPolicyAuthentication.Verify(rp,challenge,id,key,0,bad);},"nonminimal DER");
            bad=Proof(Evidence.Obj(vector["proof"]));bad.AuthenticatorData[32]=1;
            Reject(delegate{WindowsPolicyAuthentication.Verify(rp,challenge,id,key,0,bad);},"missing user verification");
            bool nativeDenied=false;try{WindowsPolicyAuthentication.Authenticate(new IntPtr(1),rp,challenge,id);}catch(InvalidOperationException){nativeDenied=true;}Check(nativeDenied,"TEST actual authenticator prohibited");
            Console.WriteLine(Evidence.Canonical(new Dictionary<string,object>{{"context","SYNTHETIC_TEST_ONLY"},{"assertions",count},{"passed",true},
                {"native_webauthn_calls",0},{"service_or_registry_calls",0},{"in_memory_public_key_cng_verification",true},{"execution_authorized",false}}));return 0;
        }
        catch(Exception error){Console.Error.WriteLine(error);return 1;}
    }
}

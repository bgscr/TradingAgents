// Headless native transfer fixture. No service, enrollment, Hello or auth records.
using System;
using System.Collections;
using System.Collections.Generic;
using System.Diagnostics;
using System.IO;
using System.Text;
using TradingAgents.PolicyOwner;
using TradingAgents.RuntimeProtection;

internal static class EvidenceTransferContracts
{
    static int Main(string[] args)
    {
        try
        {
            Native.InitializePathRuntime();
            if(args.Length != 2 || !args[1].StartsWith("test-",StringComparison.Ordinal))
                throw new InvalidDataException("Explicit disposable TEST fixture root/instance required");
            string root=Path.GetFullPath(args[0]);
            string programData=Environment.GetFolderPath(Environment.SpecialFolder.CommonApplicationData);
            if(root.StartsWith(programData+"\\",StringComparison.OrdinalIgnoreCase)
                || File.ReadAllText(Path.Combine(root,"UNAUTHENTICATED-TEST-STORE.txt"))!="NO HUMAN AUTHENTICATION OR OPERATIONAL AUTHORITY\n")
                throw new InvalidDataException("Registered/protected stores cannot be fixture targets");
            byte[] input;
            using(var memory=new MemoryStream())
            {
                var stream=Console.OpenStandardInput();byte[] block=new byte[65536];int count;
                while((count=stream.Read(block,0,block.Length))>0)
                { if(memory.Length+count>2*1024*1024)throw new InvalidDataException("Fixture frame too large");memory.Write(block,0,count); }
                input=memory.ToArray();
            }
            var request=OwnerWire.Parse(input);
            Evidence.Closed(request,"operation","payload");
            string operation=(string)request["operation"];
            var payload=Evidence.Obj(request["payload"]);
            if(operation=="fixture-validation-window")
            {
                Evidence.Closed(payload,"validation","purpose","document_sha256","at_utc");
                var at=DateTimeOffset.Parse((string)payload["at_utc"],System.Globalization.CultureInfo.InvariantCulture,System.Globalization.DateTimeStyles.RoundtripKind);
                Evidence.Require(at.Offset==TimeSpan.Zero,"Explicit fixture UTC required");
                PolicyOwnerService.CheckValidationWindow(Evidence.Obj(payload["validation"]),args[1],(string)payload["purpose"],(string)payload["document_sha256"],at.UtcDateTime);
                Console.Write("{\"publication_window_valid\":true,\"human_authenticated\":false}");return 0;
            }
            if(operation=="fixture-envelope-boundary")
            {
                Evidence.Closed(payload,"bytes");int target=Convert.ToInt32(payload["bytes"]);
                if(target<100||target>PolicyOwnerService.AuthenticationEnvelopeBytes+1)throw new InvalidDataException("Bounded envelope fixture required");
                var record=new Dictionary<string,object>{{"document",new Dictionary<string,object>()},{"padding",""}};
                record["padding"]=new String('E',target-OwnerWire.Bytes(record).Length+2);
                PolicyOwnerService.CheckAuthenticationCapacity(new Dictionary<string,object>(),record,new List<Dictionary<string,object>>{record},DateTime.UtcNow.ToString("o"));
                Console.Write("{\"envelope_boundary_validated\":true}");return 0;
            }
            if(operation=="fixture-prechallenge-capacity")
            {
                Evidence.Closed(payload,"journal_bytes","documents");
                int target=Convert.ToInt32(payload["journal_bytes"]);
                if(target<100||target>32*1024*1024)throw new InvalidDataException("Bounded current journal fixture required");
                var records=new List<Dictionary<string,object>>{new Dictionary<string,object>{{"synthetic_padding",""}}};
                records[0]["synthetic_padding"]=new String('M',target-Encoding.UTF8.GetByteCount(Evidence.Canonical(records)));
                var documents=new List<Dictionary<string,object>>();
                foreach(object item in (IEnumerable)payload["documents"])documents.Add(Evidence.Obj(item));
                var capacity=PolicyOwnerService.CapacityProjection(new Dictionary<string,object>(),records,documents,DateTime.UtcNow.ToString("o"));
                byte[] capacityReply=OwnerWire.Bytes(capacity);Console.OpenStandardOutput().Write(capacityReply,0,capacityReply.Length);return 0;
            }
            if(operation=="fixture-memory-child")
            {
                Evidence.Closed(payload);
                try {var allocation=new byte[128*1024*1024];allocation[allocation.Length-1]=1;throw new Exception("Memory ceiling did not reject this allocation");}
                catch(OutOfMemoryException) { Console.Write("{\"bounded_allocation_rejected\":true}");return 0; }
            }
            if(operation=="fixture-owned-memory")
            {
                Evidence.Closed(payload);
                string executable=Process.GetCurrentProcess().MainModule.FileName;
                var start=new ProcessStartInfo(executable){Arguments="\""+root+"\" "+args[1],UseShellExecute=false,
                    CreateNoWindow=true,RedirectStandardInput=true,RedirectStandardOutput=true,RedirectStandardError=true,WorkingDirectory=root};
                using(var children=new ProtectedChildren(64L*1024*1024))
                {
                    byte[] reply=children.RunRedirected(start,"\""+executable+"\"",OwnerWire.Bytes(new Dictionary<string,object>{
                        {"operation","fixture-memory-child"},{"payload",new Dictionary<string,object>()}}),10000);
                    var memoryResult=OwnerWire.Parse(reply);
                    Evidence.Require((bool)memoryResult["bounded_allocation_rejected"],"Memory fixture did not exercise its ceiling");
                    Evidence.Require(children.Quiesce(),"Memory fixture child not quiescent");
                    memoryResult["child_quiescent"]=true;memoryResult["operational_authority"]=false;
                    byte[] memoryReply=OwnerWire.Bytes(memoryResult);Console.OpenStandardOutput().Write(memoryReply,0,memoryReply.Length);return 0;
                }
            }
            if(operation=="fixture-journal-capacity")
            {
                Evidence.Closed(payload,"documents");
                var records=new List<Dictionary<string,object>>();
                foreach(object document in (IEnumerable)payload["documents"])
                    records.Add(new Dictionary<string,object>{{"contract_version","INVALID_SYNTHETIC_RECORD_NOT_AUTHENTICATION"},
                        {"document",document},{"proof","NO HUMAN AUTHENTICATION"},{"execution_authorized",false}});
                if(records.Count==0)throw new InvalidDataException("Fixture records required");
                var clock=Stopwatch.StartNew();
                PolicyOwnerService.CheckAuthenticationCapacity(new Dictionary<string,object>(),records[records.Count-1],records,DateTime.UtcNow.ToString("o"));
                var metrics=new Dictionary<string,object>{{"status","CAPACITY_ONLY_NOT_AUTHENTICATION"},
                    {"journal_bytes",Encoding.UTF8.GetByteCount(Evidence.Canonical(records))},
                    {"worker_request_bytes",PolicyOwnerService.WorkerRequestBytes(new Dictionary<string,object>(),"apply-authenticated-record",records[records.Count-1],records,DateTime.UtcNow.ToString("o")).Length},
                    {"elapsed_milliseconds",clock.ElapsedMilliseconds},{"records",records.Count},{"peak_working_set_bytes",Process.GetCurrentProcess().PeakWorkingSet64}};
                byte[] bytes=OwnerWire.Bytes(metrics);Console.OpenStandardOutput().Write(bytes,0,bytes.Length);return 0;
            }
            if(operation=="fixture-journal-boundary")
            {
                Evidence.Closed(payload,"bytes");int target=Convert.ToInt32(payload["bytes"]);
                if(target<1024||target>32*1024*1024+1)throw new InvalidDataException("Bounded journal fixture only");
                var records=new List<Dictionary<string,object>>{
                    new Dictionary<string,object>{{"fixture_only",true},{"padding",new String('J',target/2)}},
                    new Dictionary<string,object>{{"fixture_only",true},{"padding",""}}};
                int missing=target-Encoding.UTF8.GetByteCount(Evidence.Canonical(records));
                records[1]["padding"]=new String('K',missing);
                PolicyOwnerService.CheckAuthenticationCapacity(new Dictionary<string,object>(),records[1],records,DateTime.UtcNow.ToString("o"));
                byte[] bytes=OwnerWire.Bytes(new Dictionary<string,object>{{"status","JOURNAL_BOUNDARY_ONLY"},{"bytes",Encoding.UTF8.GetByteCount(Evidence.Canonical(records))},{"peak_working_set_bytes",Process.GetCurrentProcess().PeakWorkingSet64}});
                Console.OpenStandardOutput().Write(bytes,0,bytes.Length);return 0;
            }
            if(operation=="fixture-frame-boundary")
            {
                Evidence.Closed(payload,"bytes");int target=Convert.ToInt32(payload["bytes"]);
                if(target<1024||target>64*1024*1024+1)throw new InvalidDataException("Bounded frame fixture only");
                byte[] bytes=Encoding.ASCII.GetBytes("{\"fixture\":\""+new String('F',target-14)+"\"}");
                if(bytes.Length!=target)throw new Exception("Fixture length mismatch");
                OwnerWire.Parse(bytes);
                byte[] reply=OwnerWire.Bytes(new Dictionary<string,object>{{"status","FRAME_BOUNDARY_ONLY"},{"bytes",bytes.Length},{"peak_working_set_bytes",Process.GetCurrentProcess().PeakWorkingSet64}});
                Console.OpenStandardOutput().Write(reply,0,reply.Length);return 0;
            }
            var result=EvidenceTransfer.Dispatch(root,args[1],operation,payload,DateTime.UtcNow);
            byte[] output=OwnerWire.Bytes(result);Console.OpenStandardOutput().Write(output,0,output.Length);return 0;
        }
        catch(Exception error){Console.Error.WriteLine(error.GetType().Name+": "+error.Message);return 2;}
    }
}

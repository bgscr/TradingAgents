// Bounded local chunk transfer. Published bytes are NOT authenticated evidence.
// Only compact, separately authenticated v2 descriptors may register them.
using System;
using System.Collections.Generic;
using System.ComponentModel;
using System.Diagnostics;
using System.Globalization;
using System.IO;
using System.Runtime.InteropServices;
using System.Text.RegularExpressions;
using Microsoft.Win32.SafeHandles;
using TradingAgents.RuntimeProtection;

namespace TradingAgents.PolicyOwner
{
    internal static class EvidenceTransfer
    {
        internal const int ChunkBytes=1024*1024, DescriptorBytes=16*1024, MaximumPending=8, MaximumRecords=4096;
        internal const long ArtifactBytes=256L*1024*1024, StoreBytes=8L*1024*1024*1024;
        internal static Dictionary<string,object> Capabilities()
        {
            return new Dictionary<string,object>{{"contract_version","policy-owner-evidence-capabilities-v2"},
                {"maximum_artifact_bytes",ArtifactBytes},{"maximum_chunk_bytes",ChunkBytes},{"maximum_descriptor_bytes",DescriptorBytes},
                {"maximum_pending_imports",MaximumPending},{"maximum_transfer_records",MaximumRecords},{"maximum_store_bytes",StoreBytes},
                {"maximum_import_seconds",3600},{"native_transfer_memory_bytes",256L*1024*1024},{"native_transfer_milliseconds",30000},
                {"python_verifier_memory_bytes",4L*1024*1024*1024},{"heavy_verification_milliseconds",300000},{"authentication_journal_bytes",32*1024*1024},
                {"owner_frame_bytes",64*1024*1024},{"maximum_authentication_envelope_bytes",PolicyOwnerService.AuthenticationEnvelopeBytes},
                {"staging_is_authentication",false},{"execution_authorized",false}};
        }
        [DllImport("kernel32.dll",CharSet=CharSet.Unicode,SetLastError=true)]
        static extern bool MoveFileExW(string source,string destination,uint flags);

        static long Integer(object value)
        { Evidence.Require(value is int || value is long,"Integral transfer length/offset required");return Convert.ToInt64(value); }
        static string Token(object value,string pattern,string message)
        { var text=value as string;Evidence.Require(text!=null&&Regex.IsMatch(text,pattern),message);return text; }
        static string Id(object value)
        {return Token(value,"^[0-9a-f]{32}$","Invalid transfer identifier");}
        static Dictionary<string,object> Pin(Dictionary<string,object> reference)
        {
            Evidence.Closed(reference,"identity","pin");
            Token(reference["identity"],"^[a-z][a-z0-9-]*:v[1-9][0-9]*:sha256:[0-9a-f]{64}$","Invalid artifact identity");
            var pin=Evidence.Obj(reference["pin"]);Evidence.Closed(pin,"byte_length","sha256");
            long length=Integer(pin["byte_length"]);
            Evidence.Require(length>0&&length<=ArtifactBytes,"Artifact exceeds versioned byte bound");
            Token(pin["sha256"],"^[0-9a-f]{64}$","Invalid artifact digest");return pin;
        }
        static DateTime Time(object value)
        {
            var parsed=DateTimeOffset.Parse((string)value,CultureInfo.InvariantCulture,DateTimeStyles.RoundtripKind);
            Evidence.Require(parsed.Offset==TimeSpan.Zero,"Explicit UTC descriptor time required");return parsed.UtcDateTime;
        }
        internal static void ValidateDescriptor(string instance,Dictionary<string,object> value,DateTime now)
        {
            Evidence.Require(OwnerWire.Bytes(value).Length<=DescriptorBytes,"Compact descriptor exceeds bound");
            Evidence.Closed(value,"contract_version","operation","instance","role_identity","approval_identity","candidate",
                "lineage_plan_identity","acquisition_identity","stage","phase","artifact_schema","artifact_identity_field","artifact","issued_utc","expires_utc","identity");
            Evidence.Require((string)value["contract_version"]=="policy-owner-artifact-descriptor-v2"
                &&(string)value["operation"]=="REGISTER_EVIDENCE"&&(string)value["instance"]==instance,"Descriptor version/operation/instance mismatch");
            Token(instance,"^(operational|test)-[0-9]{8}-[0-9]{3}$","Invalid instance");
            foreach(string field in new[]{"role_identity","approval_identity","lineage_plan_identity"})
                Token(value[field],"^[a-z][a-z0-9-]*:v[1-9][0-9]*:sha256:[0-9a-f]{64}$","Descriptor context identity missing");
            var candidate=Evidence.Obj(value["candidate"]);Evidence.Closed(candidate,"commit","tree","parent");
            foreach(string field in new[]{"commit","tree","parent"})Token(candidate[field],"^[0-9a-f]{40}$","Invalid candidate binding");
            var reference=Evidence.Obj(value["artifact"]);Pin(reference);
            string schema=Token(value["artifact_schema"],"^[a-z][a-z0-9-]*:v[1-9][0-9]*$","Invalid artifact schema");
            Evidence.Require(((string)reference["identity"]).StartsWith(schema+":sha256:",StringComparison.Ordinal),"Artifact schema mismatch");
            Token(value["artifact_identity_field"],"^[a-z][a-z0-9_]{0,63}$","Invalid artifact identity field");
            Token(value["stage"],"^(CURRENT_SOURCE|ISO02|ISO03|QCF17|ISO04_TICKET06|REVIEWS|CLOSURE_HANDOFF)$","Invalid artifact stage");
            Token(value["phase"],"^(before|after|evidence)$","Invalid acquisition phase");
            string acquisition=value["acquisition_identity"] as string;
            Evidence.Require(!String.IsNullOrWhiteSpace(acquisition)&&acquisition.Length<=512,"Missing acquisition identity");
            DateTime issued=Time(value["issued_utc"]),expires=Time(value["expires_utc"]);
            Evidence.Require(issued<=now&&now<expires&&expires>issued&&(expires-issued).TotalSeconds<=3600,"Expired/future/unbounded import descriptor");
            var body=new Dictionary<string,object>(value);body.Remove("identity");
            Evidence.Require((string)value["identity"]=="policy-owner-artifact-descriptor:v2:sha256:"+Native.Hash(OwnerWire.Bytes(body)),"Descriptor identity mismatch");
        }
        static string Temp(string root){return Path.Combine(root,"state","temp");}
        static string Session(string root,string id,string suffix){return Path.Combine(Temp(root),"evidence-import-"+Id(id)+suffix);}
        static Dictionary<string,object> Load(string root,string instance,string id,DateTime now,bool active)
        {
            using(var doc=new PinnedJson(Session(root,id,".json"),null))
            {
                var value=doc.Value;Evidence.Closed(value,"transfer_id","descriptor");
                Evidence.Require((string)value["transfer_id"]==id,"Transfer identity changed");
                var descriptor=Evidence.Obj(value["descriptor"]);
                if(active)ValidateDescriptor(instance,descriptor,now);
                else Evidence.Require((string)descriptor["instance"]==instance,"Transfer instance mismatch");
                Evidence.Require(!File.Exists(Session(root,id,".cancelled")),"Transfer was cancelled");
                return descriptor;
            }
        }
        sealed class StoreLease:IDisposable
        {
            readonly List<SafeFileHandle> anchors;
            readonly SafeFileHandle handle;
            internal StoreLease(string root)
            {
                Native.LocalPath(root,false);anchors=Native.Anchor(Temp(root));
                try
                {
                    anchors.AddRange(Native.Anchor(Path.Combine(root,"artifacts")));
                    handle=Native.CreateFileW(Path.Combine(Temp(root),"evidence-transfer.lock"),0xC0000000,0,IntPtr.Zero,4,0x02200000,IntPtr.Zero);
                    if(handle.IsInvalid)throw new Win32Exception(Marshal.GetLastWin32Error(),"Evidence store busy or unavailable");
                    var info=Native.Information(handle);Evidence.Require((info.Attributes&16)==0,"Transfer lock is not a file");
                }
                catch{Dispose();throw;}
            }
            public void Dispose(){if(handle!=null)handle.Dispose();if(anchors!=null)foreach(var item in anchors)item.Dispose();}
        }
        static void Quota(string root,long requested)
        {
            long used=0,reserved=0;int pending=0,records=0,files=0;
            foreach(string directory in new[]{Path.Combine(root,"artifacts"),Temp(root)})
                foreach(string path in Directory.GetFiles(directory))
                {
                    Evidence.Require(++files<=16384,"Evidence store file-count bound reached");
                    using(var file=Native.Open(path,Native.ReadAttributes,3))
                    {var info=Native.Information(file);used+=((long)info.SizeHigh<<32)|info.SizeLow;}
                }
            foreach(string path in Directory.GetFiles(Temp(root),"evidence-import-*.json"))
            {
                Evidence.Require(++records<MaximumRecords,"Transfer record capacity exhausted");
                using(var file=new PinnedJson(path,null))
                {
                    string id=Id(file.Value["transfer_id"]);
                    if(File.Exists(Session(root,id,".complete"))||File.Exists(Session(root,id,".cancelled")))continue;
                    pending++;
                    var pin=Pin(Evidence.Obj(Evidence.Obj(file.Value["descriptor"])["artifact"]));
                    long actual=File.Exists(Session(root,id,".part"))?new FileInfo(Session(root,id,".part")).Length:0;
                    reserved+=Math.Max(0,Integer(pin["byte_length"])-actual);
                }
            }
            Evidence.Require(pending<MaximumPending,"Pending import count exhausted; explicit cancellation required");
            Evidence.Require(used+reserved+requested<=StoreBytes,"Evidence store disk quota exceeded");
            Evidence.Require(new DriveInfo(Path.GetPathRoot(root)).AvailableFreeSpace>=requested+64L*1024*1024,"Insufficient free space for bounded import");
        }
        static Dictionary<string,object> Reply(string id,string state,long offset)
        {return new Dictionary<string,object>{{"contract_version","policy-owner-evidence-transfer-v2"},{"transfer_id",id},{"state",state},{"offset",offset},{"staged_only",true},{"execution_authorized",false}};}
        static void CheckFile(string path,Dictionary<string,object> pin)
        {
            var watch=Stopwatch.StartNew();
            using(var file=Native.Open(path,Native.ReadData,1))
            {
                var info=Native.Information(file);long length=((long)info.SizeHigh<<32)|info.SizeLow;
                Evidence.Require(length==Integer(pin["byte_length"])&&length<=ArtifactBytes,"Complete artifact length mismatch");
                Evidence.Require(Native.HashFile(file)==(string)pin["sha256"],"Complete artifact digest mismatch");
            }
            Evidence.Require(watch.ElapsedMilliseconds<=30000,"Artifact hash verification exceeded finite bound");
        }
        internal static void RequireComplete(string root,string instance,string id,Dictionary<string,object> descriptor,DateTime now)
        {
            var stored=Load(root,instance,id,now,true);
            Evidence.Require(Evidence.Canonical(stored)==Evidence.Canonical(descriptor),"Staged descriptor changed");
            using(var completed=new PinnedJson(Session(root,id,".complete"),null))
            {
                Evidence.Closed(completed.Value,"transfer_id","descriptor_identity");
                Evidence.Require((string)completed.Value["transfer_id"]==id&&(string)completed.Value["descriptor_identity"]==(string)descriptor["identity"],"Transfer publication is incomplete");
            }
            var pin=Pin(Evidence.Obj(descriptor["artifact"]));
            CheckFile(Path.Combine(root,"artifacts",(string)pin["sha256"]+".blob"),pin);
        }
        internal static Dictionary<string,object> Dispatch(string root,string instance,string operation,Dictionary<string,object> payload,DateTime now)
        {
            using(var lease=new StoreLease(root))
            {
                if(operation=="evidence-verify-complete-v2")
                {
                    Evidence.Closed(payload,"transfer_id","descriptor");
                    RequireComplete(root,instance,Id(payload["transfer_id"]),Evidence.Obj(payload["descriptor"]),now);
                    return new Dictionary<string,object>{{"complete_bytes_verified",true},{"authenticated",false},{"execution_authorized",false}};
                }
                if(operation=="evidence-import-begin-v2")
                {
                    Evidence.Closed(payload,"descriptor");var descriptor=Evidence.Obj(payload["descriptor"]);
                    ValidateDescriptor(instance,descriptor,now);var pin=Pin(Evidence.Obj(descriptor["artifact"]));
                    Quota(root,Integer(pin["byte_length"]));string id=Guid.NewGuid().ToString("N");
                    PolicyOwnerService.WriteImmutable(Session(root,id,".json"),OwnerWire.Bytes(new Dictionary<string,object>{{"transfer_id",id},{"descriptor",descriptor}}),Temp(root));
                    using(var file=new FileStream(Session(root,id,".part"),FileMode.CreateNew,FileAccess.Write,FileShare.None,4096,FileOptions.WriteThrough)){file.Flush(true);}
                    return Reply(id,"PENDING_UNAUTHENTICATED",0);
                }
                if(operation=="evidence-read-chunk-v2")
                {
                    Evidence.Closed(payload,"reference","offset","count");var reference=Evidence.Obj(payload["reference"]);var pin=Pin(reference);
                    long offset=Integer(payload["offset"]),count=Integer(payload["count"]),length=Integer(pin["byte_length"]);
                    Evidence.Require(offset>=0&&count>0&&count<=ChunkBytes&&offset+count<=length,"Artifact read range exceeds bound");
                    string path=Path.Combine(root,"artifacts",(string)pin["sha256"]+".blob");
                    using(var handle=Native.Open(path,Native.ReadData,1))
                    using(var file=new FileStream(handle,FileAccess.Read))
                    {
                        Evidence.Require(file.Length==length,"Artifact read length changed");file.Position=offset;
                        byte[] bytes=new byte[(int)count];int read=0;
                        while(read<bytes.Length){int n=file.Read(bytes,read,bytes.Length-read);Evidence.Require(n>0,"Artifact short read");read+=n;}
                        return new Dictionary<string,object>{{"contract_version","policy-owner-evidence-read-v2"},{"reference",reference},{"offset",offset},{"bytes_base64",Convert.ToBase64String(bytes)},{"execution_authorized",false}};
                    }
                }
                Evidence.Require(operation=="evidence-import-chunk-v2"||operation=="evidence-import-finish-v2"||operation=="evidence-import-cancel-v2"||operation=="evidence-import-inspect-v2","Unknown versioned evidence operation");
                Evidence.Closed(payload,operation=="evidence-import-chunk-v2"?new[]{"transfer_id","offset","bytes_base64"}:new[]{"transfer_id"});
                string transfer=Id(payload["transfer_id"]);
                var saved=Load(root,instance,transfer,now,operation!="evidence-import-cancel-v2");
                var expected=Pin(Evidence.Obj(saved["artifact"]));string part=Session(root,transfer,".part");
                if(operation=="evidence-import-inspect-v2")
                    return Reply(transfer,File.Exists(Session(root,transfer,".complete"))?"PUBLISHED_UNAUTHENTICATED":"PENDING_UNAUTHENTICATED",File.Exists(part)?new FileInfo(part).Length:Integer(expected["byte_length"]));
                Evidence.Require(!File.Exists(Session(root,transfer,".complete")),"Completed transfer cannot be replayed/cancelled");
                if(operation=="evidence-import-cancel-v2")
                {
                    PolicyOwnerService.WriteImmutable(Session(root,transfer,".cancelled"),OwnerWire.Bytes(new Dictionary<string,object>{{"transfer_id",transfer},{"cancelled",true}}),Temp(root));
                    if(File.Exists(part))File.Delete(part);return Reply(transfer,"CANCELLED",0);
                }
                if(operation=="evidence-import-chunk-v2")
                {
                    string encoded=payload["bytes_base64"] as string;
                    Evidence.Require(encoded!=null&&encoded.Length<=4*((ChunkBytes+2)/3),"Chunk frame exceeds bound");
                    byte[] bytes=Convert.FromBase64String(encoded);long offset=Integer(payload["offset"]);
                    Evidence.Require(bytes.Length>0&&bytes.Length<=ChunkBytes&&offset>=0&&offset+bytes.Length<=Integer(expected["byte_length"]),"Chunk length/offset exceeds declared artifact");
                    using(var handle=Native.Open(part,0xC0000000,0))
                    using(var file=new FileStream(handle,FileAccess.ReadWrite))
                    {
                        Evidence.Require(file.Length==offset,"Chunk is stale, reordered, concurrent or replayed");
                        file.Position=offset;file.Write(bytes,0,bytes.Length);file.Flush(true);
                    }
                    return Reply(transfer,"PENDING_UNAUTHENTICATED",offset+bytes.Length);
                }
                CheckFile(part,expected);
                string destination=Path.Combine(root,"artifacts",(string)expected["sha256"]+".blob");
                if(File.Exists(destination)){CheckFile(destination,expected);File.Delete(part);}
                else if(!MoveFileExW(part,destination,8))throw new Win32Exception(Marshal.GetLastWin32Error(),"Immutable artifact publication failed");
                PolicyOwnerService.WriteImmutable(Session(root,transfer,".complete"),OwnerWire.Bytes(new Dictionary<string,object>{{"transfer_id",transfer},{"descriptor_identity",saved["identity"]}}),Temp(root));
                return Reply(transfer,"PUBLISHED_UNAUTHENTICATED",Integer(expected["byte_length"]));
            }
        }
    }
}

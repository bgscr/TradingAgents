// Read-only pre-provision validation. No Python, service, account, registry,
// authentication or ledger mutation occurs here. Uses the existing Windows DLL.
using System;
using System.Collections.Generic;
using System.ComponentModel;
using System.IO;
using System.Runtime.InteropServices;
using System.Text;
using Microsoft.Win32.SafeHandles;
using TradingAgents.RuntimeProtection;

namespace TradingAgents.PolicyOwner
{
    internal sealed class SetupDatabase : IDisposable
    {
        [DllImport("kernel32.dll",CharSet=CharSet.Unicode,SetLastError=true)] static extern IntPtr LoadLibraryExW(string path,IntPtr file,uint flags);
        [DllImport("kernel32.dll",CharSet=CharSet.Ansi,ExactSpelling=true,SetLastError=true)] static extern IntPtr GetProcAddress(IntPtr module,string name);
        [DllImport("kernel32.dll")] static extern bool FreeLibrary(IntPtr module);
        [UnmanagedFunctionPointer(CallingConvention.Cdecl)] delegate int Open(byte[] path,out IntPtr database,int flags,IntPtr vfs);
        [UnmanagedFunctionPointer(CallingConvention.Cdecl)] delegate int Prepare(IntPtr database,byte[] sql,int bytes,out IntPtr statement,IntPtr tail);
        [UnmanagedFunctionPointer(CallingConvention.Cdecl)] delegate int Unary(IntPtr value);
        [UnmanagedFunctionPointer(CallingConvention.Cdecl)] delegate int ColumnNumber(IntPtr statement,int column);
        [UnmanagedFunctionPointer(CallingConvention.Cdecl)] delegate IntPtr ColumnPointer(IntPtr statement,int column);
        readonly SafeFileHandle platform;
        IntPtr module,database;
        Prepare prepare;
        Unary step,finalize,close,columnCount;
        ColumnNumber columnType,columnBytes;
        ColumnPointer columnText;
        T Function<T>(string name) where T:class
        {
            IntPtr address=GetProcAddress(module,"sqlite3_"+name);
            if(address==IntPtr.Zero)throw new Win32Exception(Marshal.GetLastWin32Error());
            return (T)(object)Marshal.GetDelegateForFunctionPointer(address,typeof(T));
        }
        internal SetupDatabase(string path)
        {
            string dll=Path.Combine(Environment.GetFolderPath(Environment.SpecialFolder.System),"winsqlite3.dll");
            platform=Native.OpenWindowsPlatformImage(dll);
            try
            {
                module=LoadLibraryExW(dll,IntPtr.Zero,0x900); // DLL_LOAD_DIR | SYSTEM32
                if(module==IntPtr.Zero)throw new Win32Exception(Marshal.GetLastWin32Error());
                close=Function<Unary>("close");prepare=Function<Prepare>("prepare_v2");
                step=Function<Unary>("step");finalize=Function<Unary>("finalize");columnCount=Function<Unary>("column_count");
                columnType=Function<ColumnNumber>("column_type");columnBytes=Function<ColumnNumber>("column_bytes");columnText=Function<ColumnPointer>("column_text");
                string uri=new Uri(Native.LocalPath(path,true)).AbsoluteUri+"?mode=ro&immutable=1";
                Evidence.Require(Function<Open>("open_v2")(Encoding.UTF8.GetBytes(uri+"\0"),out database,1|0x40,IntPtr.Zero)==0,"Cannot read staged setup database");
                Query("PRAGMA trusted_schema=OFF");Query("PRAGMA query_only=ON");Query("PRAGMA temp_store=MEMORY");
            }
            catch {Dispose();throw;}
        }
        internal List<object[]> Query(string sql)
        {
            IntPtr statement;
            Evidence.Require(prepare(database,Encoding.UTF8.GetBytes(sql+"\0"),-1,out statement,IntPtr.Zero)==0,"Invalid staged database/schema");
            try
            {
                var rows=new List<object[]>();int status;
                while((status=step(statement))==100)
                {
                    Evidence.Require(rows.Count<10000,"Setup database exceeds bounded intake");
                    var row=new object[columnCount(statement)];
                    for(int i=0;i<row.Length;i++)
                    {
                        if(columnType(statement,i)==5) {row[i]=null;continue;}
                        IntPtr pointer=columnText(statement,i);int length=columnBytes(statement,i);
                        Evidence.Require(length>=0&&length<=64*1024*1024,"Staged database value exceeds contract");
                        byte[] raw=new byte[length];if(length!=0)Marshal.Copy(pointer,raw,0,length);
                        row[i]=new UTF8Encoding(false,true).GetString(raw);
                    }
                    rows.Add(row);
                }
                Evidence.Require(status==101,"Staged database read failed");return rows;
            }
            finally {finalize(statement);}
        }
        public void Dispose()
        {
            if(database!=IntPtr.Zero&&close!=null) {close(database);database=IntPtr.Zero;}
            if(module!=IntPtr.Zero) {FreeLibrary(module);module=IntPtr.Zero;}
            if(platform!=null)platform.Dispose();
        }
    }

    internal static class SetupStateValidator
    {
        // Exact sqlite_master(type,name,tbl_name,sql) content generated by
        // initialize_owner_state and LineageLedger.provision. Root page numbers
        // are deliberately excluded. Changes require a reviewed schema update.
        const string OwnerSchema="c385edb204710b0559709eae0c5a1dd5ed958fc3e5ef2f38bed35f9af2ecea66";
        const string LineageSchema="6284b993cb7f9dd40bdcf76591757a3d2630d35b2bab7711dd0a5a98b220e2eb";
        internal static void FileAllowed(string relative,string instance)
        {
            bool test=System.Text.RegularExpressions.Regex.IsMatch(instance,@"\Atest-[0-9]{8}-[0-9]{3}\z");
            bool allowed=relative=="bin/PolicyOwner.exe"||relative=="bin/ConditionalRuntimeProtectionHost.exe"
                ||relative=="worker-manifest.json"||relative=="original-acquisitions.json"||relative=="role-proposal.json"
                ||relative=="native-consumer-source.json"||relative=="state/owner.sqlite3"||relative=="state/lineage.sqlite3"
                ||relative.StartsWith("python/",StringComparison.Ordinal)||relative.StartsWith("source/",StringComparison.Ordinal)
                ||relative.StartsWith("dependencies/",StringComparison.Ordinal)
                ||(test&&(relative=="bin/PolicyAuthenticationContracts.exe"
                    ||relative.StartsWith("test-client-dependencies/",StringComparison.Ordinal)))
                ||System.Text.RegularExpressions.Regex.IsMatch(relative,@"\Aartifacts/[0-9a-f]{64}\.blob\z");
            Evidence.Require(allowed,"Setup cannot import authentication, temporary or unregistered state files: "+relative);
        }
        static void Schema(SetupDatabase database,string expected)
        {
            var schema=database.Query("SELECT type,name,tbl_name,sql FROM sqlite_master ORDER BY type,name");
            Evidence.Require(Native.Hash(Encoding.UTF8.GetBytes(Evidence.Canonical(schema)))==expected,"Staged database schema differs from the implemented contract");
            Evidence.Require(Evidence.Canonical(database.Query("PRAGMA quick_check"))=="[[\"ok\"]]","Staged database is corrupt");
        }
        internal static void Validate(string depot,string instance,string ledger,Dictionary<string,Dictionary<string,object>> entries)
        {
            foreach(string relative in entries.Keys)FileAllowed(relative,instance);
            using(var owner=new SetupDatabase(Native.Child(depot,"state/owner.sqlite3")))
            using(var lineage=new SetupDatabase(Native.Child(depot,"state/lineage.sqlite3")))
            {
                Schema(owner,OwnerSchema);Schema(lineage,LineageSchema);
                Evidence.Require(Evidence.Canonical(owner.Query("SELECT instance,ledger_id,last_clock FROM metadata"))
                    ==Evidence.Canonical(new object[]{new object[]{instance,ledger,""}}),"Setup owner metadata must have an unset clock and the exact registration");
                Evidence.Require(Evidence.Canonical(lineage.Query("SELECT schema_version,ledger_id FROM metadata"))
                    ==Evidence.Canonical(new object[]{new object[]{"policy-qualified-lineage-ledger-v1",ledger}}),"Setup lineage registration mismatch");
                foreach(string table in new string[]{"approvals","applied_records"})
                    Evidence.Require(owner.Query("SELECT * FROM "+table+" LIMIT 1").Count==0,"Setup cannot import an approval or authentication effect");
                foreach(string table in new string[]{"claims","events","invalidations","allocations"})
                    Evidence.Require(lineage.Query("SELECT * FROM "+table+" LIMIT 1").Count==0,"Setup cannot import consumed lineage state");
                var artifacts=new Dictionary<string,Dictionary<string,object>>(StringComparer.Ordinal);
                var blobs=new HashSet<string>(StringComparer.Ordinal);
                foreach(var row in owner.Query("SELECT identity,reference FROM artifacts"))
                {
                    string raw=(string)row[1];
                    // EvidenceRef.to_bytes is canonical JSON plus one LF.
                    Evidence.Require(raw.EndsWith("\n",StringComparison.Ordinal),"Noncanonical staged artifact reference");
                    var reference=OwnerWire.Parse(Encoding.UTF8.GetBytes(raw.Substring(0,raw.Length-1)));
                    Evidence.Closed(reference,"identity","pin");var pin=Evidence.Obj(reference["pin"]);Evidence.Closed(pin,"sha256","byte_length");
                    string identity=(string)row[0],relative="artifacts/"+(string)pin["sha256"]+".blob";
                    Evidence.Require(identity==(string)reference["identity"]&&!artifacts.ContainsKey(identity)&&entries.ContainsKey(relative),"Staged artifact identity is missing or duplicated");
                    Evidence.Require((string)entries[relative]["sha256"]==(string)pin["sha256"]&&Convert.ToInt64(entries[relative]["byte_length"])==Convert.ToInt64(pin["byte_length"]),"Staged artifact reference differs from bundle bytes");
                    artifacts.Add(identity,reference);blobs.Add(relative);
                }
                foreach(string relative in entries.Keys)
                    if(relative.StartsWith("artifacts/",StringComparison.Ordinal))Evidence.Require(blobs.Contains(relative),"Unregistered setup artifact blob");
                using(var registry=new PinnedJson(Native.Child(depot,"original-acquisitions.json"),(string)entries["original-acquisitions.json"]["sha256"]))
                {
                    var value=registry.Value;Evidence.CheckIdentity(value,"policy-owner-original-acquisition-registry:v1");
                    Evidence.Closed(value,"contract_version","source_package_manifest_pin","original_blocker","before","after","complete_delta_ledger","acquisition_registration_mode","newly_acquired_observations_eligible","identity");
                    Evidence.Require((string)value["contract_version"]=="policy-owner-original-acquisition-registry-v1"
                        &&(string)value["acquisition_registration_mode"]=="EXACT_RETAINED_ORIGINAL_PAIR_ONLY"&&!(bool)value["newly_acquired_observations_eligible"],"Unknown original registry contract");
                    foreach(object item in new object[]{value["original_blocker"],value["complete_delta_ledger"],Evidence.Obj(value["before"])["artifact"],Evidence.Obj(value["after"])["artifact"]})
                    {
                        var reference=Evidence.Obj(item);string identity=(string)reference["identity"];
                        Evidence.Require(artifacts.ContainsKey(identity)&&Evidence.Canonical(artifacts[identity])==Evidence.Canonical(reference),"Original registry evidence is not preloaded exactly");
                    }
                }
            }
        }
    }
}

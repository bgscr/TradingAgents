using System;
using System.IO;
using System.Diagnostics;
using System.Runtime.InteropServices;
using Microsoft.Win32.SafeHandles;
using TradingAgents.RuntimeProtection;

public static class NativeContracts
{
    // Only an ignored, instrumented test copy of the host calls this barrier.
    public static void WaitForAdmission()
    {
        string prefix=Environment.GetEnvironmentVariable("TA_TEST_ADMISSION");
        if(prefix==null)return;
        if(prefix.IndexOf("\\build\\rp-retained\\",StringComparison.Ordinal)<0)throw new Exception("Barrier must stay in retained fixture scope");
        using(var stream=new FileStream(prefix+"."+Process.GetCurrentProcess().Id+".ready",FileMode.CreateNew)) { }
        var timer=Stopwatch.StartNew();
        while(!File.Exists(prefix+".release"))
        { if(timer.ElapsedMilliseconds>30000)throw new Exception("Fixture admission barrier timed out");System.Threading.Thread.Sleep(10); }
    }
    [DllImport("kernel32.dll")] static extern IntPtr GetStdHandle(int kind);
    [DllImport("kernel32.dll")] static extern bool SetStdHandle(int kind,IntPtr value);
    [DllImport("kernel32.dll")] static extern bool GetConsoleMode(IntPtr handle,out uint mode);
    [DllImport("kernel32.dll",CharSet=CharSet.Unicode,SetLastError=true)]
    static extern SafeFileHandle CreateFileW(string path,uint access,uint share,IntPtr security,uint disposition,uint flags,IntPtr template);
    static bool CanOpenWriteDac(string path)
    {
        try { using(var handle=Native.Open(path,Native.WriteDac,7)) { }return true; }
        catch(System.ComponentModel.Win32Exception error) { if(error.NativeErrorCode==5)return false;throw; }
    }
    public static int Main(string[] args)
    {
        try
        {
            Native.InitializePathRuntime();
            if(args.Length>0 && (args[0]=="host"||args[0]=="host-dll"))
            {
                // The Python test harness deliberately injects a persistence
                // guard PYTHONPATH. This native-only wrapper clears ambient
                // injection before testing the actual platform entrypoint.
                foreach(string key in new string[]{"PYTHONPATH","PYTHONHOME","PYTHONPYCACHEPREFIX"})
                    Environment.SetEnvironmentVariable(key,null);
                var forwarded=new string[args.Length-1];Array.Copy(args,1,forwarded,0,forwarded.Length);
                if(args[0]=="host-dll")Native.SetDllDirectoryW(Path.GetDirectoryName(Process.GetCurrentProcess().MainModule.FileName));
                try { return RuntimeProtectionHost.Main(forwarded); }
                finally { Native.SetDllDirectoryW(null); }
            }
            if(args.Length!=2) throw new Exception("Invalid fixture invocation");
            string root=Path.GetFullPath(args[1]);
            if(args[0]=="console-child")
            {
                uint inputMode,outputMode;
                return GetConsoleMode(GetStdHandle(-10),out inputMode)&&GetConsoleMode(GetStdHandle(-11),out outputMode)?0:42;
            }
            if(args[0]=="console-parent")
            {
                IntPtr oldInput=GetStdHandle(-10),oldOutput=GetStdHandle(-11),oldError=GetStdHandle(-12);
                using(var input=CreateFileW("CONIN$",0xc0000000,3,IntPtr.Zero,3,0,IntPtr.Zero))
                using(var output=CreateFileW("CONOUT$",0xc0000000,3,IntPtr.Zero,3,0,IntPtr.Zero))
                {
                    if(input.IsInvalid||output.IsInvalid)throw new Exception("Fixture requires a hidden parent console");
                    try
                    {
                        SetStdHandle(-10,input.DangerousGetHandle());SetStdHandle(-11,output.DangerousGetHandle());SetStdHandle(-12,output.DangerousGetHandle());
                        using(var children=new ProtectedChildren())
                        {
                            var info=new ProcessStartInfo { FileName=Process.GetCurrentProcess().MainModule.FileName,
                                Arguments="console-child \""+root+"\"",WorkingDirectory=root,UseShellExecute=false,CreateNoWindow=false };
                            using(var child=children.Start(info,"\""+info.FileName+"\""))
                            {
                                child.WaitForExit();if(children.ExitCode(child)!=0)throw new Exception("Interactive child lost its inherited console");
                            }
                            if(!children.Quiesce())throw new Exception("Console fixture process remained active");
                        }
                    }
                    finally
                    {
                        SetStdHandle(-10,oldInput);SetStdHandle(-11,oldOutput);SetStdHandle(-12,oldError);
                        Console.SetOut(new StreamWriter(Console.OpenStandardOutput()) { AutoFlush=true });
                        Console.SetError(new StreamWriter(Console.OpenStandardError()) { AutoFlush=true });
                    }
                }
                Console.WriteLine("CONTRACT_PASS: inherited interactive console preserved");return 0;
            }
            if(args[0]=="observe")
            {
                Console.WriteLine(Native.Hash(System.Text.Encoding.UTF8.GetBytes(Fingerprint(root))));
                Console.WriteLine(CanOpenWriteDac(root)?"WRITE_DAC_OPEN":"WRITE_DAC_DENIED");return 0;
            }
            if(args[0]=="inert")
            {
                if(File.ReadAllBytes(root)[0]!=42)throw new Exception("Inert bytes differ");
                return 0;
            }
            if(Directory.Exists(root)||File.Exists(root)) throw new Exception("Fixture must be new");
            if(args[0]=="stranded-successor")
            {
                var start=new ProcessStartInfo(Process.GetCurrentProcess().MainModule.FileName,
                    "strand \""+root+"\"") { UseShellExecute=false,CreateNoWindow=true };
                using(var child=Process.Start(start))
                { child.WaitForExit(); if(child.ExitCode!=37)throw new Exception("Abnormal fixture child failed"); }
                string before=Fingerprint(root);
                if(CanOpenWriteDac(root))throw new Exception("Stranded WRITE_DAC reopened");
                using(var next=FreshProtectionTree.Create(root+"-B"))
                {
                    next.WriteNewFile("payload.txt",new byte[]{42});
                    var baseline=next.CaptureBaseline();
                    using(var protection=next.Protect(baseline))
                    {
                        var inert=new ProcessStartInfo(Process.GetCurrentProcess().MainModule.FileName,
                            "inert \""+root+"-B\\payload.txt\"") { UseShellExecute=false,CreateNoWindow=true };
                        using(var child=Process.Start(inert))
                        { child.WaitForExit(); if(child.ExitCode!=0)throw new Exception("Inert B child failed"); }
                    }
                    if(!next.MatchesBaseline(baseline))throw new Exception("B did not restore its own baseline");
                }
                if(Fingerprint(root)!=before)throw new Exception("Successor mutated stranded A");
                bool rejected=false;
                try { using(var reuse=FreshProtectionTree.Create(root)) { } }
                catch(InvalidOperationException) { rejected=true; }
                if(!rejected)throw new Exception("Stranded attempt reused");
                Console.WriteLine("CONTRACT_PASS: V22 stranded A preserved; fresh B succeeds without recovering A");
                return 0;
            }
            using(var preparation=FreshProtectionTree.Create(root))
            {
                if(args[0]=="long-path")
                {
                    string relative="nested-"+new string('a',80)+"/nested-"+new string('b',80)+"/payload.txt";
                    if(Path.Combine(root,relative.Replace('/',Path.DirectorySeparatorChar)).Length<=260)
                        throw new Exception("Fixture must exercise a path longer than MAX_PATH");
                    preparation.WriteNewFile(relative,System.Text.Encoding.UTF8.GetBytes("INERT LONG PATH"));
                    var longBaseline=preparation.CaptureBaseline();
                    using(var protection=preparation.Protect(longBaseline)) { protection.VerifyProtected(); }
                    if(!preparation.MatchesBaseline(longBaseline))throw new Exception("Long-path baseline did not restore exactly");
                    Console.WriteLine("CONTRACT_PASS: long-path creation, complete inventory and exact restoration");return 0;
                }
                preparation.WriteNewFile("nested/payload.txt", System.Text.Encoding.UTF8.GetBytes("SEALED"));
                if(args[0]=="foreign-addition"||args[0]=="prebaseline-drift")
                {
                    File.WriteAllText(Path.Combine(root,args[0]=="foreign-addition"?"foreign.py":"nested/payload.txt"),"drift");
                    bool rejected=false;
                    try { preparation.CaptureBaseline(); } catch(InvalidOperationException) { rejected=true; }
                    if(!rejected)throw new Exception("An externally changed object became a fresh baseline");
                    Console.WriteLine("CONTRACT_PASS: creation provenance enforced");return 0;
                }
                var baseline=preparation.CaptureBaseline();
                if(args[0]=="cleanup-failure")
                {
                    var protector=preparation.Protect(baseline);
                    var field=typeof(ProtectedRuntimeTreeV3).GetField("entries",System.Reflection.BindingFlags.NonPublic|System.Reflection.BindingFlags.Instance);
                    var entries=(System.Collections.IList)field.GetValue(protector);
                    var first=entries[0];
                    ((IDisposable)first.GetType().GetField("Handle",System.Reflection.BindingFlags.NonPublic|System.Reflection.BindingFlags.Instance).GetValue(first)).Dispose();
                    bool failed=false;
                    try { protector.Dispose(); } catch(AggregateException) { failed=true; }
                    if(!failed||protector.Restored)throw new Exception("Cleanup failure reported success");
                    if(!CanOpenWriteDac(Path.Combine(root,"nested","payload.txt")))throw new Exception("Later protector cleanup was skipped");
                    Console.WriteLine("CONTRACT_PASS: cleanup aggregated; every later object restored");return 0;
                }
                if(args[0]=="preopened-write-dac")
                {
                    // Existing V2 trust boundary: ACL changes cannot revoke a
                    // previously granted WRITE_DAC handle. Retained read handles
                    // still deny writes; explicit DACL verification detects drift.
                    using(var held=Native.Open(Path.Combine(root,"nested"),Native.WriteDac,3))
                    using(var protector=preparation.Protect(baseline))
                    {
                        Native.SetDacl(held,Native.FromSddl("D:P(A;;FA;;;"+System.Security.Principal.WindowsIdentity.GetCurrent().User.Value+")"));
                        bool failed=false;
                        try { protector.VerifyProtected(); } catch(InvalidOperationException) { failed=true; }
                        if(!failed)throw new Exception("Pre-granted descriptor drift not detected");
                    }
                    if(!preparation.MatchesBaseline(baseline))throw new Exception("Retained current-process restoration authority lost");
                    Console.WriteLine("CONTRACT_PASS: current owner detects descriptor drift and restores exactly");return 0;
                }
                if(args[0]=="baseline-drift")
                {
                    File.WriteAllText(Path.Combine(root,"nested/payload.txt"),"DRIFT!");
                    bool rejected=false;
                    try { using(var protector=preparation.Protect(baseline)) { } }
                    catch(InvalidOperationException) { rejected=true; }
                    if(!rejected)throw new Exception("Content drift before sealing accepted");
                    if(!CanOpenWriteDac(root))throw new Exception("Partial constructor left root sealed");
                    Console.WriteLine("CONTRACT_PASS: drift rejected and acquired handles cleaned");return 0;
                }
                using(var protector=preparation.Protect(baseline))
                {
                    if(!protector.IsSealed) throw new Exception("Not sealed");
                    if(CanOpenWriteDac(root)) throw new Exception("WRITE_DAC reopened");
                    if(args[0]=="strand")
                    { Console.Out.Flush(); Environment.Exit(37); }
                }
                if(!preparation.MatchesBaseline(baseline)) throw new Exception("Exact descriptor identity was not restored");
                bool consumedRejected=false;
                try { using(var again=preparation.Protect(baseline)) { } }
                catch(InvalidOperationException) { consumedRejected=true; }
                if(!consumedRejected) throw new Exception("Consumed baseline accepted");
            }
            Console.WriteLine("EXACT_BASELINE_RESTORED_AND_CONSUMED");
            return 0;
        }
        catch(Exception error) { Console.Error.WriteLine(error); return 1; }
    }
    static string Fingerprint(string root)
    {
        // OS read-only observations, including every descendant's descriptor,
        // content and last-write metadata; never a new protection baseline.
        var paths=FreshProtectionTree.Walk(root);
        paths.Sort(StringComparer.Ordinal);
        var text=new System.Text.StringBuilder();
        foreach(string path in paths)
        {
            // Query the same descriptor/content/last-write fields through
            // read-only handles; path-based Framework ACL APIs reject long
            // payload members even when the protected object itself exists.
            using(var handle=Native.Open(path,Native.ReadData|Native.ReadAttributes|Native.ReadControl,1))
            {
                var info=Native.Information(handle);bool directory=(info.Attributes&0x10)!=0;
                System.Security.AccessControl.FileSystemSecurity sd=directory
                    ?(System.Security.AccessControl.FileSystemSecurity)new System.Security.AccessControl.DirectorySecurity()
                    :new System.Security.AccessControl.FileSecurity();
                sd.SetSecurityDescriptorBinaryForm(Native.Descriptor(handle));
                text.Append(path).Append(Convert.ToBase64String(sd.GetSecurityDescriptorBinaryForm()));
                long written=unchecked((long)(((ulong)info.WriteHigh<<32)|info.WriteLow));
                text.Append(DateTime.FromFileTimeUtc(written).Ticks);
                if(!directory)using(var stream=new FileStream(handle,FileAccess.Read))
                    using(var bytes=new MemoryStream()) { stream.CopyTo(bytes);text.Append(Convert.ToBase64String(bytes.ToArray())); }
            }
        }
        return text.ToString();
    }
}

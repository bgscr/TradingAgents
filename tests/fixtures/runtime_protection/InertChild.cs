using System;
using System.Diagnostics;
using System.Runtime.InteropServices;
using Microsoft.Win32.SafeHandles;

public static class InertChild
{
    [StructLayout(LayoutKind.Sequential)] struct ProcessBasic
    { internal IntPtr R1,Peb,R2,R3,Pid,Parent; }
    [DllImport("ntdll.dll")] static extern int NtQueryInformationProcess(IntPtr process,int kind,out ProcessBasic value,int size,out int returned);
    [DllImport("kernel32.dll",CharSet=CharSet.Unicode,SetLastError=true)]
    static extern SafeFileHandle CreateFileW(string path,uint access,uint share,IntPtr security,uint disposition,uint flags,IntPtr template);
    public static int Main(string[] arguments)
    {
        if(arguments.Length<3||arguments[0]!="-I"||arguments[1]!="-B")return 2;
        if(Environment.GetEnvironmentVariable("TRADINGAGENTS_OUTPUT_LANGUAGE")!="English")return 3;
        if(Environment.GetEnvironmentVariable("PYTHON_DOTENV_DISABLED")!="1")return 4;
        if(Environment.GetEnvironmentVariable("PYTHONPATH")!=null)return 5;
        string fixtureCredential=Environment.GetEnvironmentVariable("FIXTURE_API_KEY");
        if(fixtureCredential!=null&&fixtureCredential!="ticket07-inert-presence-only-no-network")return 10;
        if(arguments.Length==5&&arguments[3]=="--fixture-hold")
        {
            string prefix=arguments[4];
            if(prefix.IndexOf("\\build\\rp-retained\\",StringComparison.Ordinal)<0)return 11;
            using(var stream=new System.IO.FileStream(prefix+".started",System.IO.FileMode.CreateNew)) { }
            var timer=Stopwatch.StartNew();
            while(!System.IO.File.Exists(prefix+".release"))
            { if(timer.ElapsedMilliseconds>30000)return 12;System.Threading.Thread.Sleep(10); }
        }
        if(arguments.Length==5&&arguments[3]=="--fixture-strand-owner")
        {
            using(var handle=CreateFileW(arguments[4],0x40000,7,IntPtr.Zero,3,0x02200000,IntPtr.Zero))
                if(!handle.IsInvalid||Marshal.GetLastWin32Error()!=5)return 6;
            ProcessBasic basic;int returned;
            if(NtQueryInformationProcess(Process.GetCurrentProcess().Handle,0,out basic,Marshal.SizeOf(typeof(ProcessBasic)),out returned)!=0)return 7;
            using(var parent=Process.GetProcessById((int)basic.Parent))
            {
                if(!parent.MainModule.FileName.EndsWith("\\native-contract-host\\NativeContracts.exe",StringComparison.Ordinal)
                    ||parent.MainModule.FileName.IndexOf("\\build\\rp-retained\\",StringComparison.Ordinal)<0)return 8;
                Console.Out.Flush();parent.Kill();
                // The parent's job closes and kills this child. No restore runs.
                System.Threading.Thread.Sleep(10000);return 9;
            }
        }
        return 0;
    }
}

// Pure native consumer fixture. No owner, service, human proof, child or storage mutation.
using System;
using TradingAgents.RuntimeProtection;

public static class CoverageEvidenceContracts
{
    public static int Main(string[] args)
    {
        try
        {
            if(args.Length!=2)throw new ArgumentException("exact envelope file and SHA-256 required");
            using(var json=new PinnedJson(args[0],args[1]))Evidence.CoverageEnvelope(json.Value);
            Console.WriteLine("{\"status\":\"NATIVE_COVERAGE_STRUCTURE_VERIFIED\",\"owner_called\":false,\"authentication\":false,\"execution_authorized\":false}");
            return 0;
        }
        catch(Exception error)
        {
            Console.Error.WriteLine(error.GetType().Name+": "+error.Message);
            return 2;
        }
    }
}

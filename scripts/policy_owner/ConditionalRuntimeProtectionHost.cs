// Forward native consumer. Legacy RuntimeProtectionHost.Main never selects this
// owner or converts a conditional artifact into a legacy contract.
using System;
using System.Collections.Generic;
using System.Globalization;
using System.Reflection;
using System.Security.Principal;
using System.Text.RegularExpressions;
using TradingAgents.RuntimeProtection;

namespace TradingAgents.PolicyOwner
{
    internal sealed class NativeConditionalOwner : IConditionalProtectionOwner, IDisposable
    {
        readonly OwnerAnchor anchor;
        readonly string sourceIdentity;
        readonly Dictionary<string, object> binding;
        internal NativeConditionalOwner(string instance)
        {
            anchor = new OwnerAnchor(instance);
            try
            {
                Evidence.Require(WindowsIdentity.GetCurrent().User.Value == anchor.OperatorSid, "Conditional native caller differs from the registered project principal");
                foreach(AssemblyMetadataAttribute attribute in Assembly.GetExecutingAssembly().GetCustomAttributes(typeof(AssemblyMetadataAttribute), false))
                    if(attribute.Key == "PolicyQualifiedNativeConsumerSourceIdentity") sourceIdentity = attribute.Value;
                Evidence.Require(sourceIdentity != null && Regex.IsMatch(sourceIdentity, "^policy-qualified-native-consumer-source:v1:sha256:[0-9a-f]{64}$"),
                    "Complete conditional native consumer source binding is absent");
                binding = new Dictionary<string, object> {{"contract_version", "policy-qualified-native-owner-binding-v1"}, {"instance", anchor.Instance},
                    {"storage_binding_identity", anchor.Value["storage_binding_identity"]}, {"native_owner_sha256", anchor.Value["native_sha256"]},
                    {"worker_manifest_sha256", anchor.Value["worker_manifest_sha256"]}, {"native_consumer_source_identity", sourceIdentity}, {"execution_authorized", false}};
            }
            catch { anchor.Dispose(); throw; }
        }
        public Dictionary<string, object> Binding { get { return new Dictionary<string, object>(binding); } }
        public int MaximumChildSeconds { get; private set; }
        Dictionary<string, object> Call(string operation, Dictionary<string, object> payload)
        {
            payload.Add("native_consumer_source_identity", sourceIdentity);
            return OwnerWire.Call(anchor, new Dictionary<string, object> {{"operation", "worker"}, {"payload", new Dictionary<string, object> {
                {"operation", operation}, {"payload", payload}}}});
        }
        static DateTimeOffset Utc(object value)
        {
            string text = Evidence.Str(value); DateTimeOffset result;
            Evidence.Require((text.EndsWith("Z", StringComparison.Ordinal) || text.EndsWith("+00:00", StringComparison.Ordinal))
                && DateTimeOffset.TryParse(text, CultureInfo.InvariantCulture, DateTimeStyles.None, out result), "Owner check requires explicit UTC");
            return DateTimeOffset.Parse(text, CultureInfo.InvariantCulture);
        }
        void CompleteBinding(Dictionary<string, object> response, Dictionary<string, object> attempt, params string[] extra)
        {
            var fields = new List<string>(new string[]{"contract_version", "instance", "storage_binding_identity", "envelope", "status", "authority_state", "authority_state_reference",
                "checked_at_utc", "expires_utc", "execution_expires_utc", "current_candidate", "current_production_inventory_identity", "ledger_identity", "ledger_path",
                "terminal_consumption", "receipt_identity", "execution_authorized", "ticket07_or_live_authorized"});
            fields.AddRange(extra); Evidence.Closed(response, fields.ToArray());
            var inputs = Evidence.Obj(attempt["inputs"]); var envelope = Evidence.Obj(inputs["acceptance"]);
            var core = Evidence.Obj(envelope["core"]); var receipt = Evidence.Obj(envelope["receipt"]); var history = Evidence.Obj(core["history"]);
            var candidate = Evidence.Obj(core["candidate"]);
            DateTimeOffset now = DateTimeOffset.UtcNow, checkedAt = Utc(response["checked_at_utc"]);
            Evidence.Require((string)response["contract_version"] == "policy-owner-native-conditional-verification-v1"
                && (string)response["instance"] == anchor.Instance && Object.Equals(response["storage_binding_identity"], anchor.Value["storage_binding_identity"])
                && (string)response["status"] == "CONDITIONALLY_ACCEPTED_UNRESOLVED_HISTORY" && (string)response["authority_state"] == "ACTIVE"
                && !String.IsNullOrEmpty((string)response["authority_state_reference"])
                && !(bool)response["execution_authorized"] && !(bool)response["ticket07_or_live_authorized"]
                && !(bool)envelope["execution_authorized"] && !(bool)core["execution_authorized"] && !(bool)receipt["execution_authorized"]
                && Evidence.Canonical(response["envelope"]) == Evidence.Canonical(envelope)
                && Evidence.Canonical(response["current_candidate"]) == Evidence.Canonical(candidate)
                && Object.Equals(inputs["source_commit"], candidate["commit"]) && Object.Equals(inputs["source_tree"], candidate["tree"])
                && Object.Equals(response["expires_utc"], core["expires_utc"]) && checkedAt <= now && now - checkedAt <= TimeSpan.FromSeconds(5)
                && now < Utc(response["expires_utc"]) && now < Utc(response["execution_expires_utc"])
                && Object.Equals(response["ledger_identity"], anchor.Value["ledger_identity"])
                && (string)response["ledger_path"] == System.IO.Path.Combine(anchor.Root, "state", "lineage.sqlite3")
                && Regex.IsMatch((string)response["terminal_consumption"], "^policy-qualified-lineage-event:v1:sha256:[0-9a-f]{64}$")
                && Object.Equals(response["receipt_identity"], receipt["receipt_identity"])
                && (string)history["historical_claim"] == "UNRESOLVED_RAW_DIFFERENCE" && !(bool)history["historical_equal"]
                && (string)history["historical_cause"] == "UNRESOLVED" && (string)history["prior_outcome"] == "BLOCKED",
                "Fresh owner verification lost authority, source, consumption, expiry or the complete unresolved-history caveat");
        }
        public void VerifyAttempt(Dictionary<string, object> attempt)
        {
            var result = Call("verify-attempt", new Dictionary<string, object> {{"attempt", attempt}});
            CompleteBinding(result, attempt, "attempt");
            Evidence.Require(Evidence.Canonical(result["attempt"]) == Evidence.Canonical(attempt), "Owner verified a different complete attempt");
        }
        public void VerifyMaterialization(Dictionary<string, object> plan)
        {
            var result = Call("verify-materialization", new Dictionary<string, object> {{"materialization", plan}});
            CompleteBinding(result, Evidence.Obj(plan["attempt"]), "materialization_identity");
            Evidence.Require(Object.Equals(result["materialization_identity"], plan["identity"]), "Owner verified different native materialization bytes");
        }
        public void RequireInertLaunch(Dictionary<string, object> prepared, Dictionary<string, object> packet)
        {
            Evidence.Require(!anchor.Operational, "Operational conditional launch requires separately valid Ticket-07/live authorization; no such authority is issued here");
            var result = Call("verify-inert-launch", new Dictionary<string, object> {{"prepared", prepared}, {"control", packet}});
            CompleteBinding(result, Evidence.Obj(prepared["attempt"]), "control_identity", "maximum_child_seconds", "inert_registration_identity");
            Evidence.Require(Object.Equals(result["control_identity"], packet["identity"])
                && !String.IsNullOrEmpty((string)result["inert_registration_identity"]), "No exact authenticated inert fixture authorization");
            MaximumChildSeconds = Convert.ToInt32(result["maximum_child_seconds"]);
            Evidence.Require(MaximumChildSeconds > 0 && MaximumChildSeconds <= 120, "Inert fixture time budget is invalid");
        }
        public void Dispose() { anchor.Dispose(); }
    }

    public static class ConditionalRuntimeProtectionHost
    {
        public static int Main(string[] args)
        {
            try
            {
                Native.InitializePathRuntime();
                Evidence.Require(args.Length == 4 || args.Length == 6, "Expected operation, registered owner instance, and exact pinned native inputs");
                using(var owner = new NativeConditionalOwner(args[1]))
                using(var input = new PinnedJson(args[2], args[3]))
                {
                    Dictionary<string, object> result;
                    if(args.Length == 4 && args[0] == "verify")
                    { owner.VerifyAttempt(input.Value); result = new Dictionary<string, object> {{"attempt", input.Value}, {"owner_binding", owner.Binding}, {"execution_authorized", false}}; }
                    else if(args.Length == 4 && args[0] == "prepare") result = RuntimeProtectionHost.Prepare(input.Value, owner);
                    else if(args.Length == 6 && args[0] == "run-inert")
                    { using(var control = new PinnedJson(args[4], args[5])) result = RuntimeProtectionHost.Run(input.Value, control.Value, owner); }
                    else throw new InvalidOperationException("Unsupported conditional operation; no legacy downgrade or live authorization flow exists");
                byte[] output=OwnerWire.Bytes(result);var stdout=Console.OpenStandardOutput();stdout.Write(output,0,output.Length);stdout.Flush();return 0;
                }
            }
            catch(Exception error) { Console.Error.WriteLine(error); return 2; }
        }
    }
}

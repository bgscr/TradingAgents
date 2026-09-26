using System;
using System.Collections.Generic;
using System.ComponentModel;
using System.Diagnostics;
using System.IO;
using System.Globalization;
using System.Runtime.InteropServices;
using System.Security.Principal;
using System.ServiceProcess;
using System.Text;
using System.Windows.Forms;
using Microsoft.Win32.SafeHandles;
using TradingAgents.RuntimeProtection;

namespace TradingAgents.PolicyOwner
{
    public static class PolicyOwnerMain
    {
        [DllImport("advapi32.dll", SetLastError=true)]
        static extern bool SetKernelObjectSecurity(IntPtr handle, uint information, byte[] descriptor);
        [DllImport("advapi32.dll", SetLastError=true)]
        static extern bool OpenProcessToken(IntPtr process, uint access, out SafeAccessTokenHandle token);
        static void Output(Dictionary<string, object> value)
        { byte[] raw=OwnerWire.Bytes(value);var stream=Console.OpenStandardOutput();stream.Write(raw,0,raw.Length);stream.Flush(); }

        static Dictionary<string, object> Proof(WindowsPolicyAuthentication.Proof proof)
        {
            return new Dictionary<string, object> {{"credential_id", Convert.ToBase64String(proof.CredentialId)},
                {"authenticator_data", Convert.ToBase64String(proof.AuthenticatorData)}, {"signature", Convert.ToBase64String(proof.Signature)},
                {"client_data", Convert.ToBase64String(proof.ClientData)}};
        }
        static void ExposeFrontendIdentity(OwnerAnchor anchor)
        {
            using(var identity = WindowsIdentity.GetCurrent())
            {
                Evidence.Require(identity.User.Value == anchor.OperatorSid && Native.TokenHasEnabledSid(identity.AccessToken, "S-1-5-32-544")
                    && OwnerWire.IntegrityLevel(identity.AccessToken) >= 0x3000, "Use the operator's elevated native frontend; no role follows from elevation alone");
                string baseAcl = "D:P(A;;GA;;;SY)(A;;GA;;;BA)(A;;GA;;;" + identity.User.Value + ")";
                using(var process = Process.GetCurrentProcess())
                {
                    SafeAccessTokenHandle token;
                    if(!OpenProcessToken(process.Handle, 0x00040008, out token)) throw new Win32Exception(Marshal.GetLastWin32Error());
                    using(token)
                        if(!SetKernelObjectSecurity(token.DangerousGetHandle(), 0x80000004, Native.FromSddl(baseAcl + "(A;;0x00020008;;;" + anchor.ServiceSid + ")")))
                            throw new Win32Exception(Marshal.GetLastWin32Error());
                    if(!SetKernelObjectSecurity(process.Handle, 0x80000004, Native.FromSddl(baseAcl + "(A;;0x00101000;;;" + anchor.ServiceSid + ")")))
                        throw new Win32Exception(Marshal.GetLastWin32Error());
                }
            }
        }
        static Dictionary<string, object> HumanDecision(OwnerAnchor anchor, string purpose, Dictionary<string, object> document)
        {
            Evidence.Require(String.Equals(Process.GetCurrentProcess().MainModule.FileName, anchor.NativePath, StringComparison.OrdinalIgnoreCase),
                "Human frontend must be the registered protected native binary");
            ExposeFrontendIdentity(anchor);
            Dictionary<string, object> result = null;
            int requestMilliseconds=document.ContainsKey("contract_version")&&((string)document["contract_version"]).EndsWith("-v2",StringComparison.Ordinal)?360000:60000;
            using(var form = new Form())
            using(var content = new TextBox())
            using(var consent = new CheckBox())
            using(var authenticate = new Button())
            using(var cancel = new Button())
            using(var panel = new FlowLayoutPanel())
            {
                form.Text = "TradingAgents — " + (anchor.Operational ? "Operational owner" : "ISOLATED TEST owner") + " — " + purpose;
                form.Width = 1000; form.Height = 760; form.StartPosition = FormStartPosition.CenterScreen;
                content.Multiline = true; content.ReadOnly = true; content.ScrollBars = ScrollBars.Both; content.WordWrap = true; content.Dock = DockStyle.Fill;
                content.Text = "Principal: " + anchor.OperatorSid + Environment.NewLine + "Instance: " + anchor.Instance + Environment.NewLine
                    + "Storage binding: " + (string)anchor.Value["storage_binding_identity"] + Environment.NewLine
                    + "Operation: " + purpose + Environment.NewLine
                    + (purpose == "ENROLL_PROJECT_ROLE" ? "Enrollment registers only the displayed role. It approves no package, starts no qualification clock and authorizes no execution."
                        : "This authentication applies to the exact document below. A policy result itself never grants Ticket-07 or live authority.")
                    + Environment.NewLine + CompleteArtifactReview(anchor,document)
                    + Environment.NewLine + Environment.NewLine + Evidence.Canonical(document);
                panel.Dock = DockStyle.Bottom; panel.Height = 100; panel.FlowDirection = FlowDirection.LeftToRight;
                consent.Text = "I have reviewed this exact document and intend this specific operation."; consent.Width = 910; consent.Height = 35;
                authenticate.Text = "Authenticate with Windows Hello"; authenticate.Width = 320; authenticate.Enabled = false;
                cancel.Text = "Cancel"; cancel.DialogResult = DialogResult.Cancel;
                consent.CheckedChanged += delegate { authenticate.Enabled = consent.Checked; };
                authenticate.Click += delegate {
                    authenticate.Enabled = false; consent.Enabled = false;
                    try
                    {
                        var challenge = OwnerWire.Call(anchor, new Dictionary<string, object> {{"operation", "challenge"}, {"payload", new Dictionary<string, object> {
                            {"purpose", purpose}, {"document", document}}}},requestMilliseconds);
                        string rp = (string)challenge["rp_id"];
                        byte[] bytes = Convert.FromBase64String((string)challenge["challenge"]);
                        Dictionary<string, object> registration = null; byte[] credentialId;
                        if(purpose == "ENROLL_PROJECT_ROLE")
                        {
                            var created = WindowsPolicyAuthentication.Create(form.Handle, rp, bytes, anchor.OperatorSid,anchor.Instance);
                            credentialId = created.CredentialId;
                            registration = new Dictionary<string, object> {{"id", Convert.ToBase64String(created.CredentialId)},
                                {"authenticator_data", Convert.ToBase64String(created.AuthenticatorData)}, {"public_key", Convert.ToBase64String(created.PublicKey)}};
                        }
                        else credentialId = Convert.FromBase64String((string)challenge["credential_id"]);
                        var proof = WindowsPolicyAuthentication.Authenticate(form.Handle, rp, bytes, credentialId);
                        result = OwnerWire.Call(anchor, new Dictionary<string, object> {{"operation", "complete-authentication"}, {"payload", new Dictionary<string, object> {
                            {"challenge_id", challenge["challenge_id"]}, {"registration", registration}, {"proof", Proof(proof)}}}},requestMilliseconds);
                        form.DialogResult = DialogResult.OK; form.Close();
                    }
                    catch(Exception error)
                    {
                        MessageBox.Show(form, error.Message + "\nNo new approval is inferred from a cancelled or failed authentication.", "Owner rejected operation", MessageBoxButtons.OK, MessageBoxIcon.Error);
                        form.DialogResult = DialogResult.Abort; form.Close();
                    }
                };
                panel.Controls.Add(consent); panel.Controls.Add(authenticate); panel.Controls.Add(cancel);
                form.Controls.Add(content); form.Controls.Add(panel); form.CancelButton = cancel;
                Evidence.Require(form.ShowDialog() == DialogResult.OK && result != null, "Human operation was cancelled or rejected");
                return result;
            }
        }
        static string CompleteArtifactReview(OwnerAnchor anchor,Dictionary<string,object> document)
        {
            return EvidenceTransfer.CompleteArtifactReview(anchor.Root,document);
        }
        [STAThread]
        public static int Main(string[] args)
        {
            try
            {
                Native.InitializePathRuntime();
                Evidence.Require(IntPtr.Size == 8, "64-bit Windows owner required");
                if(args.Length==1&&args[0]=="evidence-capabilities")
                {Output(EvidenceTransfer.Capabilities());return 0;}
                if(args.Length == 1 && args[0] == "probe")
                { Output(WindowsPolicyAuthentication.Probe()); return 0; }
                if(args.Length == 3 && args[0] == "setup")
                { Output(PolicyOwnerSetup.Interactive(args[1],args[2]));return 0; }
                if(args.Length == 2 && args[0] == "service")
                { ServiceBase.Run(new PolicyOwnerService(args[1])); return 0; }
                if(args.Length==2&&args[0]=="transfer-worker-v2")
                {
                    using(var identity=WindowsIdentity.GetCurrent())
                    {
                        Evidence.Require(identity.User.Value.StartsWith("S-1-5-80-",StringComparison.Ordinal),"Transfer worker requires the dedicated service account");
                        using(var anchor=new OwnerAnchor(args[1]))
                        {
                            Evidence.Require(identity.User.Value==anchor.ServiceSid&&String.Equals(Process.GetCurrentProcess().MainModule.FileName,anchor.NativePath,StringComparison.OrdinalIgnoreCase),"Transfer worker instance/image/token mismatch");
                            using(var memory=new MemoryStream())
                            {
                                var input=Console.OpenStandardInput();byte[] buffer=new byte[65536];int read;
                                while((read=input.Read(buffer,0,buffer.Length))>0){Evidence.Require(memory.Length+read<=2*1024*1024,"Transfer control frame too large");memory.Write(buffer,0,read);}
                                var request=OwnerWire.Parse(memory.ToArray());Evidence.Closed(request,"operation","payload");
                                Output(EvidenceTransfer.Dispatch(anchor.Root,anchor.Instance,(string)request["operation"],Evidence.Obj(request["payload"]),DateTime.UtcNow));return 0;
                            }
                        }
                    }
                }
                Evidence.Require(args.Length == 2 || args.Length == 4, "Expected registered instance and explicit owner operation");
                using(var anchor = new OwnerAnchor(args[1]))
                {
                    Dictionary<string, object> result;
                    if(args.Length == 2 && args[0] == "request")
                    {
                        using(var memory = new MemoryStream())
                        {
                            var input = Console.OpenStandardInput(); byte[] buffer = new byte[65536]; int read;
                            while((read = input.Read(buffer, 0, buffer.Length)) != 0)
                            { Evidence.Require(memory.Length + read <= 64 * 1024 * 1024, "Owner request too large"); memory.Write(buffer, 0, read); }
                            var request = OwnerWire.Parse(memory.ToArray()); Evidence.Closed(request, "operation", "payload");
                            Evidence.Require((string)request["operation"] != "validate-authentication-document" && (string)request["operation"] != "apply-authenticated-record",
                                "Authentication is available only through the visible Windows Hello flow");
                            result = OwnerWire.Call(anchor, new Dictionary<string, object> {{"operation", "worker"}, {"payload", request}},((string)request["operation"]).StartsWith("evidence-",StringComparison.Ordinal)?90000:60000);
                        }
                    }
                    else if(args.Length == 2 && args[0] == "status")
                        result = OwnerWire.Call(anchor, new Dictionary<string, object> {{"operation", "status"}, {"payload", new Dictionary<string, object>()}});
                    else
                    {
                        Evidence.Require(args.Length == 4, "Human action requires the exact document path and whole-file SHA-256");
                        var purposes = new Dictionary<string, string> {{"enroll", "ENROLL_PROJECT_ROLE"}, {"decide", "EXACT_POLICY_DECISION"},
                            {"revoke", "REVOKE_DECISION"}, {"register-evidence", "REGISTER_EVIDENCE"}};
                        Evidence.Require(purposes.ContainsKey(args[0]), "Unsupported native owner operation");
                        using(var document = new PinnedJson(args[2], args[3])) result = HumanDecision(anchor, purposes[args[0]], document.Value);
                    }
                    Output(result); return 0;
                }
            }
            catch(Exception error) { Console.Error.WriteLine(error.GetType().Name + ": " + error.Message); return 2; }
        }
    }
}

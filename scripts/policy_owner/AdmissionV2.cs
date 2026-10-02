// Enrollment-only v2. No policy decision, reconciliation, qualification or
// generic worker operation is reachable from this entrypoint.
using System;
using System.Collections.Generic;
using System.IO;
using System.Text;
using System.Windows.Forms;
using System.ServiceProcess;
using TradingAgents.RuntimeProtection;

namespace TradingAgents.PolicyOwner
{
    internal interface IAdmissionRpc
    {
        Dictionary<string,object> Call(string operation,Dictionary<string,object> payload);
    }
    internal interface IAdmissionAuthenticator
    {
        WindowsPolicyAuthentication.Registration Create(IntPtr window,string rp,byte[] challenge,string sid,byte[] handle,Action<Dictionary<string,object>> boundary);
        WindowsPolicyAuthentication.Proof Authenticate(IntPtr window,string rp,byte[] challenge,byte[] id);
    }
    internal sealed class AdmissionAuthenticator : IAdmissionAuthenticator
    {
        public WindowsPolicyAuthentication.Registration Create(IntPtr window,string rp,byte[] challenge,string sid,byte[] handle,Action<Dictionary<string,object>> boundary)
        { return WindowsPolicyAuthentication.CreateV2(window,rp,challenge,sid,handle,boundary); }
        public WindowsPolicyAuthentication.Proof Authenticate(IntPtr window,string rp,byte[] challenge,byte[] id)
        { return WindowsPolicyAuthentication.Authenticate(window,rp,challenge,id); }
    }
    internal static class AdmissionV2
    {
        internal const string Mode="ENROLLMENT_ONLY";
        internal static bool PublicOperation(string value)
        { return value=="service-v2"||value=="enroll-v2"; }
        internal static bool WorkerOperation(string value)
        { return value=="inspect-v2"||value=="reserve-v2"||value=="begin-creation-v2"||value=="accept-v2"||value=="verify-complete-v2"; }
        internal static bool RpcOperation(string value)
        { return WorkerOperation(value)||value=="creation-boundary-v2"; }
        internal static int Entry(string[] args)
        {
            // Validate the complete command grammar before touching Windows,
            // the registry, a file, a pipe, or a credential API.
            if(args.Length!=2||!PublicOperation(args[0]))
            { Console.Error.WriteLine("ENROLLMENT_ONLY v2 rejects legacy/unknown entrypoints"); return 2; }
#if OWNER_ADMISSION_V2_TEST
            Console.Error.WriteLine("TEST assembly cannot enter formal platform paths");return 2;
#else
            try
            {
                Native.InitializePathRuntime();
                if(args[0]=="service-v2") { ServiceBase.Run(new AdmissionV2Service(args[1]));return 0; }
                using(var anchor=new AdmissionV2Anchor(args[1]))
                {
                    AdmissionV2Platform.ExposeOwnProcess(anchor,true);
                    var result=Show(anchor,new AdmissionV2Rpc(anchor),new AdmissionAuthenticator());
                    byte[] bytes=OwnerWire.Bytes(result);var stream=Console.OpenStandardOutput();stream.Write(bytes,0,bytes.Length);stream.Flush();return 0;
                }
            }
            catch(Exception error) { Console.Error.WriteLine(error.GetType().Name+": "+error.Message);return 2; }
#endif
        }
        internal static Dictionary<string,object> Complete(IAdmissionRpc rpc,IAdmissionAuthenticator authenticator,
            IntPtr window,string sid,Dictionary<string,object> reserved)
        {
            long epoch=Convert.ToInt64(reserved["epoch"]);
            string attempt=(string)reserved["attempt_id"];
            // This call must commit CREATION_BEGUN. A response loss leaves the
            // generation consumed; this function never retries any step.
            var begun=rpc.Call("begin-creation-v2",new Dictionary<string,object>{{"expected_epoch",epoch},{"attempt",attempt}});
            Evidence.Require((string)begun["status"]=="CREATION_BEGUN"&&(string)begun["attempt"]==attempt,
                "Durable creation intent was not acknowledged");
            byte[] handle=Convert.FromBase64String((string)reserved["user_handle"]);
            Evidence.Require(handle.Length==32&&Object.Equals(begun["user_handle"],reserved["user_handle"]),"Credential generation changed");
            string rp=(string)reserved["rp_id"];byte[] challenge=Convert.FromBase64String((string)reserved["challenge"]);
            var registration=authenticator.Create(window,rp,challenge,sid,handle,delegate(Dictionary<string,object> parameters)
            {
                var observed=rpc.Call("creation-boundary-v2",new Dictionary<string,object>{{"expected_epoch",epoch},{"attempt",attempt},
                    {"creation_receipt",begun["creation_receipt"]},{"parameters",parameters}});
                Evidence.Require((string)observed["status"]=="BOUNDARY_RECORDED"&&Object.Equals(observed["creation_receipt"],begun["creation_receipt"]),"Actual boundary observation was not persisted");
            });
            var proof=authenticator.Authenticate(window,rp,challenge,registration.CredentialId);
            var accepted=rpc.Call("accept-v2",new Dictionary<string,object>{{"expected_epoch",epoch},{"attempt",attempt},
                {"registration",new Dictionary<string,object>{{"id",Convert.ToBase64String(registration.CredentialId)},
                    {"authenticator_data",Convert.ToBase64String(registration.AuthenticatorData)},{"public_key",Convert.ToBase64String(registration.PublicKey)}}},
                {"proof",new Dictionary<string,object>{{"credential_id",Convert.ToBase64String(proof.CredentialId)},
                    {"authenticator_data",Convert.ToBase64String(proof.AuthenticatorData)},{"signature",Convert.ToBase64String(proof.Signature)},
                    {"client_data",Convert.ToBase64String(proof.ClientData)}}}});
            Evidence.Require((string)accepted["phase"]=="COMPLETE_STOP","Enrollment did not reach its terminal phase");
            return rpc.Call("verify-complete-v2",new Dictionary<string,object>());
        }
#if !OWNER_ADMISSION_V2_TEST
        static Dictionary<string,object> Show(AdmissionV2Anchor anchor,IAdmissionRpc rpc,IAdmissionAuthenticator authenticator)
        {
            Dictionary<string,object> result=null;
            using(var form=new Form())using(var content=new TextBox())using(var consent=new CheckBox())
            using(var action=new Button())using(var cancel=new Button())using(var panel=new FlowLayoutPanel())
            {
                form.Text="TradingAgents — same-instance enrollment v2";form.Width=1000;form.Height=760;
                content.Multiline=true;content.ReadOnly=true;content.ScrollBars=ScrollBars.Both;content.WordWrap=false;content.Dock=DockStyle.Fill;
                content.Text="This registers only the frozen project role. It authorizes no execution.\r\n"
                    +"Historical failures and UNKNOWN outcomes remain unchanged.\r\nFULL ORIGINAL ROLE:\r\n"+anchor.RoleDocument
                    +"\r\nHISTORICAL SETUP, NEW IMPLEMENTATION AND BUSINESS IDENTITIES:\r\n"+Evidence.Canonical(anchor.Binding);
                panel.Dock=DockStyle.Bottom;panel.Height=100;consent.Width=900;
                consent.Text="I reviewed the exact business candidate, role, new code identity and same-instance mapping.";
                action.Text="Inspect enrollment or reserve";action.Width=300;action.Enabled=false;cancel.Text="Cancel";
                cancel.DialogResult=DialogResult.Cancel;consent.CheckedChanged+=delegate{action.Enabled=consent.Checked;};
                action.Click+=delegate
                {
                    action.Enabled=false;consent.Enabled=false;
                    try
                    {
                        var current=rpc.Call("inspect-v2",new Dictionary<string,object>());
                        if((string)current["phase"]=="COMPLETE_STOP")
                        {
                            result=rpc.Call("verify-complete-v2",new Dictionary<string,object>());form.DialogResult=DialogResult.OK;form.Close();return;
                        }
                        Evidence.Require((string)current["phase"]=="CLOSED"&&(string)current["classification"]=="ELIGIBLE"&&current["attempt"]==null,
                            "No fresh enrollment is admitted from active, partial or blocked state");
                        var reserve=rpc.Call("reserve-v2",new Dictionary<string,object>{{"expected_epoch",current["epoch"]},
                            {"deadline_utc",DateTime.UtcNow.AddMinutes(4).ToString("yyyy-MM-ddTHH:mm:ss.ffffffZ",System.Globalization.CultureInfo.InvariantCulture)}});
                        content.Text+="\r\nEXACT ATTEMPT:\r\n"+Evidence.Canonical(reserve);content.Refresh();
                        if(!ConfirmAttempt(form,anchor.RoleDocument,reserve))
                            throw new OperationCanceledException("Reserved attempt remains closed until terminal classification");
                        result=Complete(rpc,authenticator,form.Handle,anchor.OperatorSid,reserve);form.DialogResult=DialogResult.OK;form.Close();
                    }
                    catch(Exception error){MessageBox.Show(form,error.Message+"\r\nNo automatic retry or credential recreation is permitted.");form.DialogResult=DialogResult.Abort;form.Close();}
                };
                panel.Controls.Add(consent);panel.Controls.Add(action);panel.Controls.Add(cancel);form.Controls.Add(content);form.Controls.Add(panel);form.CancelButton=cancel;
                Evidence.Require(form.ShowDialog()==DialogResult.OK&&result!=null,"Enrollment was not completed");return result;
            }
        }
        static bool ConfirmAttempt(Form parent,string role,Dictionary<string,object> reserved)
        {
            using(var form=new Form())using(var text=new TextBox())using(var consent=new CheckBox())using(var create=new Button())using(var cancel=new Button())using(var panel=new FlowLayoutPanel())
            {
                form.Text="Review exact v2 attempt before credential creation";form.Width=1000;form.Height=760;
                text.Multiline=true;text.ReadOnly=true;text.ScrollBars=ScrollBars.Both;text.WordWrap=false;text.Dock=DockStyle.Fill;
                text.Text="FULL ORIGINAL ROLE:\r\n"+role+"\r\nDURABLE ATTEMPT, CUT/STATE AND CODE BINDING:\r\n"+Evidence.Canonical(reserved);
                panel.Dock=DockStyle.Bottom;panel.Height=100;consent.Width=900;consent.Text="I reviewed this exact attempt and original role; create this new credential generation.";
                create.Text="Create and authenticate";create.Width=300;create.Enabled=false;create.DialogResult=DialogResult.OK;
                cancel.Text="Cancel";cancel.DialogResult=DialogResult.Cancel;consent.CheckedChanged+=delegate{create.Enabled=consent.Checked;};
                panel.Controls.Add(consent);panel.Controls.Add(create);panel.Controls.Add(cancel);form.Controls.Add(text);form.Controls.Add(panel);form.CancelButton=cancel;
                return form.ShowDialog(parent)==DialogResult.OK;
            }
        }
#endif
    }
}

// Windows platform WebAuthn adapter. No registration, approval or role is implicit.
// The trusted owner supplies a fresh challenge and separately registers the public
// credential after an explicitly authorized setup. This file never stores a key.
using System;
using System.Collections.Generic;
using System.IO;
using System.Runtime.InteropServices;
using System.Security.Cryptography;
using System.Security.Principal;
using System.Text;

[assembly: DefaultDllImportSearchPaths(DllImportSearchPath.System32)]

namespace TradingAgents.PolicyOwner
{
    internal static class WindowsPolicyAuthentication
    {
        internal const string OperationalRp = "tradingagents-policy-owner.local";
        internal const string TestRp = "tradingagents-policy-owner-test.local";

        [StructLayout(LayoutKind.Sequential, CharSet=CharSet.Unicode)]
        struct Rp { internal uint Version; internal string Id, Name, Icon; }
        [StructLayout(LayoutKind.Sequential, CharSet=CharSet.Unicode)]
        struct User { internal uint Version, Size; internal IntPtr Id; internal string Name, Icon, DisplayName; }
        [StructLayout(LayoutKind.Sequential, CharSet=CharSet.Unicode)]
        struct Parameter { internal uint Version; internal string Type; internal int Algorithm; }
        [StructLayout(LayoutKind.Sequential)]
        struct Array { internal uint Count; internal IntPtr Values; }
        [StructLayout(LayoutKind.Sequential, CharSet=CharSet.Unicode)]
        struct ClientData { internal uint Version, Size; internal IntPtr Json; internal string HashAlgorithm; }
        // Version 1 layouts are intentionally used. Later trailing fields are not
        // read or supplied; no dependency on a downloaded SDK or a newer ABI.
        [StructLayout(LayoutKind.Sequential)]
        struct MakeOptions
        {
            internal uint Version, Timeout;
            internal Array Exclusions, Extensions;
            internal uint Attachment;
            internal int Resident;
            internal uint Verification, Attestation, Flags;
        }
        [StructLayout(LayoutKind.Sequential, CharSet=CharSet.Unicode)]
        struct Credential { internal uint Version, Size; internal IntPtr Id; internal string Type; }
        [StructLayout(LayoutKind.Sequential, CharSet=CharSet.Unicode)]
        struct GetOptions
        {
            internal uint Version, Timeout;
            internal Array Credentials, Extensions;
            internal uint Attachment, Verification, Flags;
            internal string AppId;
            internal IntPtr UsedAppId;
        }
        [StructLayout(LayoutKind.Sequential, CharSet=CharSet.Unicode)]
        struct Attestation
        {
            internal uint Version; internal string Format;
            internal uint AuthSize; internal IntPtr Auth;
            internal uint StatementSize; internal IntPtr Statement;
            internal uint DecodeType; internal IntPtr Decoded;
            internal uint ObjectSize; internal IntPtr Object;
            internal uint CredentialSize; internal IntPtr Credential;
            internal Array Extensions;
        }
        [StructLayout(LayoutKind.Sequential)]
        struct Assertion
        {
            internal uint Version, AuthSize; internal IntPtr Auth;
            internal uint SignatureSize; internal IntPtr Signature;
            internal Credential Credential;
            internal uint UserSize; internal IntPtr User;
        }
        [DllImport("webauthn.dll", ExactSpelling=true)]
        static extern uint WebAuthNGetApiVersionNumber();
        [DllImport("webauthn.dll", ExactSpelling=true)]
        static extern int WebAuthNIsUserVerifyingPlatformAuthenticatorAvailable([MarshalAs(UnmanagedType.Bool)] out bool available);
        [DllImport("webauthn.dll", ExactSpelling=true, CharSet=CharSet.Unicode)]
        static extern int WebAuthNAuthenticatorMakeCredential(IntPtr window, ref Rp rp, ref User user,
            ref Array parameters, ref ClientData data, ref MakeOptions options, out IntPtr result);
        [DllImport("webauthn.dll", ExactSpelling=true, CharSet=CharSet.Unicode)]
        static extern int WebAuthNAuthenticatorGetAssertion(IntPtr window, string rp,
            ref ClientData data, ref GetOptions options, out IntPtr result);
        [DllImport("webauthn.dll", ExactSpelling=true)]
        static extern void WebAuthNFreeCredentialAttestation(IntPtr value);
        [DllImport("webauthn.dll", ExactSpelling=true)]
        static extern void WebAuthNFreeAssertion(IntPtr value);

        internal static Dictionary<string, object> Probe()
        {
            bool available;
            int status = WebAuthNIsUserVerifyingPlatformAuthenticatorAvailable(out available);
            return new Dictionary<string, object> {
                {"api_version", WebAuthNGetApiVersionNumber()},
                {"availability_hresult", status}, {"platform_available", status == 0 && available},
                {"observed_sid", WindowsIdentity.GetCurrent().User.Value},
                {"observed_name", WindowsIdentity.GetCurrent().Name},
                {"observed_token_has_enabled_administrators", new WindowsPrincipal(WindowsIdentity.GetCurrent()).IsInRole(WindowsBuiltInRole.Administrator)},
                {"human_authenticated", false}, {"principal_enrolled", false},
                {"execution_authorized", false}
            };
        }

        sealed class Buffer : IDisposable
        {
            internal readonly IntPtr Pointer;
            internal Buffer(byte[] value)
            {
                Pointer = Marshal.AllocHGlobal(value.Length);
                Marshal.Copy(value, 0, Pointer, value.Length);
            }
            public void Dispose() { Marshal.FreeHGlobal(Pointer); }
        }
        sealed class Structure<T> : IDisposable
        {
            internal readonly IntPtr Pointer;
            internal Structure(T value)
            {
                Pointer = Marshal.AllocHGlobal(Marshal.SizeOf(typeof(T)));
                Marshal.StructureToPtr(value, Pointer, false);
            }
            public void Dispose()
            { Marshal.DestroyStructure(Pointer, typeof(T)); Marshal.FreeHGlobal(Pointer); }
        }
        static byte[] Copy(IntPtr pointer, uint size)
        {
            Require(size <= 65536 && (size == 0 || pointer != IntPtr.Zero), "Invalid WebAuthn output size");
            byte[] result = new byte[size];
            if(size != 0) Marshal.Copy(pointer, result, 0, result.Length);
            return result;
        }
        internal static byte[] Hash(byte[] bytes)
        { using(var hash = SHA256.Create()) return hash.ComputeHash(bytes); }
        internal static bool Equal(byte[] a, byte[] b)
        {
            if(a.Length != b.Length) return false;
            int difference = 0;
            for(int i = 0; i < a.Length; i++) difference |= a[i] ^ b[i];
            return difference == 0;
        }
        internal static void Require(bool value, string reason)
        { if(!value) throw new InvalidDataException(reason); }
        internal static string Base64Url(byte[] bytes)
        { return Convert.ToBase64String(bytes).TrimEnd('=').Replace('+', '-').Replace('/', '_'); }
        static void RpContract(string rp)
        { Require(rp == OperationalRp || rp == TestRp, "Unknown local project relying party"); }
        internal static byte[] ClientJson(string rp, byte[] challenge, bool creation)
        {
            RpContract(rp);
            Require(challenge.Length == 32, "A fresh 256-bit owner challenge is required");
            return Encoding.UTF8.GetBytes("{\"type\":\"webauthn." + (creation ? "create" : "get") +
                "\",\"challenge\":\"" + Base64Url(challenge) + "\",\"origin\":\"https://" + rp + "\",\"crossOrigin\":false}");
        }

        internal sealed class Registration
        {
            internal byte[] CredentialId, AuthenticatorData, PublicKey;
        }
        internal sealed class Proof
        {
            internal byte[] CredentialId, AuthenticatorData, Signature, ClientData;
        }
        internal static byte[] RegistrationUserHandle(string rpId,string expectedSid,string instance)
        {
            RpContract(rpId);
            Require(System.Text.RegularExpressions.Regex.IsMatch(instance,@"\A(?:operational|test)-[0-9]{8}-[0-9]{3}\z"),"Unknown enrollment instance");
            Require(new SecurityIdentifier(expectedSid).Value==expectedSid,"Invalid enrollment SID");
            Require((rpId==TestRp)==instance.StartsWith("test-",StringComparison.Ordinal),"Enrollment RP and instance differ");
            // A shared RP/user handle can replace an existing discoverable
            // credential. Bind new enrollment to this exact instance instead.
            return Hash(Encoding.UTF8.GetBytes("policy-owner-user-handle-v1\n"+rpId+"\n"+expectedSid+"\n"+instance));
        }
        internal static Registration Create(IntPtr window, string rpId, byte[] challenge, string expectedSid,string instance)
        {
            // Only an interactive, operator-invoked frontend calls this method.
            // SID equality is a scope check; it is never the human proof itself.
            Require(window != IntPtr.Zero, "A visible operator window is required");
            Require(WindowsIdentity.GetCurrent().User.Value == expectedSid, "Observed principal differs from proposed enrollment");
            byte[] client = ClientJson(rpId, challenge, true);
            byte[] userId = RegistrationUserHandle(rpId,expectedSid,instance);
            using(var userBuffer = new Buffer(userId))
            using(var json = new Buffer(client))
            using(var parameter = new Structure<Parameter>(new Parameter {Version=1, Type="public-key", Algorithm=-7}))
            {
                var rp = new Rp {Version=1, Id=rpId, Name="TradingAgents project policy owner"};
                var user = new User {Version=1, Size=(uint)userId.Length, Id=userBuffer.Pointer,
                    Name=expectedSid, DisplayName=expectedSid};
                var parameters = new Array {Count=1, Values=parameter.Pointer};
                var data = new ClientData {Version=1, Size=(uint)client.Length, Json=json.Pointer, HashAlgorithm="SHA-256"};
                var options = new MakeOptions {Version=1, Timeout=120000, Attachment=1,
                    Verification=1, Attestation=1}; // PLATFORM, REQUIRED, NONE
                IntPtr output;
                int status = WebAuthNAuthenticatorMakeCredential(window, ref rp, ref user, ref parameters, ref data, ref options, out output);
                if(status != 0) Marshal.ThrowExceptionForHR(status);
                try
                {
                    var value = (Attestation)Marshal.PtrToStructure(output, typeof(Attestation));
                    byte[] auth = Copy(value.Auth, value.AuthSize);
                    byte[] credential = Copy(value.Credential, value.CredentialSize);
                    return new Registration {CredentialId=credential, AuthenticatorData=auth,
                        PublicKey=RegistrationPublicKey(rpId, auth, credential)};
                }
                finally { WebAuthNFreeCredentialAttestation(output); }
            }
        }
        internal static Proof Authenticate(IntPtr window, string rpId, byte[] challenge, byte[] credentialId)
        {
            Require(window != IntPtr.Zero, "A visible operator window is required");
            Require(credentialId.Length > 0 && credentialId.Length <= 4096, "Invalid credential ID");
            byte[] client = ClientJson(rpId, challenge, false);
            using(var id = new Buffer(credentialId))
            using(var json = new Buffer(client))
            using(var credential = new Structure<Credential>(new Credential {Version=1,
                Size=(uint)credentialId.Length, Id=id.Pointer, Type="public-key"}))
            {
                var data = new ClientData {Version=1, Size=(uint)client.Length, Json=json.Pointer, HashAlgorithm="SHA-256"};
                var options = new GetOptions {Version=1, Timeout=120000, Credentials=new Array {Count=1, Values=credential.Pointer}, Attachment=1, Verification=1};
                IntPtr output;
                int status = WebAuthNAuthenticatorGetAssertion(window, rpId, ref data, ref options, out output);
                if(status != 0) Marshal.ThrowExceptionForHR(status);
                try
                {
                    var value = (Assertion)Marshal.PtrToStructure(output, typeof(Assertion));
                    return new Proof {CredentialId=Copy(value.Credential.Id, value.Credential.Size),
                        AuthenticatorData=Copy(value.Auth, value.AuthSize), Signature=Copy(value.Signature, value.SignatureSize), ClientData=client};
                }
                finally { WebAuthNFreeAssertion(output); }
            }
        }

        static byte[] Slice(byte[] bytes, int offset, int length)
        {
            Require(offset >= 0 && length >= 0 && offset <= bytes.Length - length, "Truncated authenticator data");
            byte[] value = new byte[length]; System.Array.Copy(bytes, offset, value, 0, length); return value;
        }
        static uint Header(string rp, byte[] auth, bool registration)
        {
            RpContract(rp);
            Require(auth.Length >= 37 && Equal(Slice(auth, 0, 32), Hash(Encoding.UTF8.GetBytes(rp))), "Relying-party hash mismatch");
            // Deliberately support no extensions, backup credentials or unknown
            // flags. An unsupported platform response fails closed.
            Require(auth[32] == (registration ? 0x45 : 0x05), "User presence/verification or authenticator flags invalid");
            return ((uint)auth[33] << 24) | ((uint)auth[34] << 16) | ((uint)auth[35] << 8) | auth[36];
        }
        internal static byte[] RegistrationPublicKey(string rp, byte[] auth, byte[] credential)
        {
            Header(rp, auth, true);
            Require(auth.Length >= 55, "Missing attested credential data");
            int length = (auth[53] << 8) | auth[54];
            Require(length > 0 && length <= 4096 && Equal(Slice(auth, 55, length), credential), "Attested credential ID mismatch");
            int offset = 55 + length;
            // Closed COSE EC2 / ES256 / P-256 map; field order may vary. No general
            // CBOR decoder or alternate algorithm is accepted at this boundary.
            Require(offset < auth.Length && auth[offset++] == 0xa5, "Expected closed five-member COSE key");
            var fields = new HashSet<int>(); byte[] x = null, y = null;
            for(int i = 0; i < 5; i++)
            {
                Require(offset < auth.Length, "Truncated COSE key");
                int key = auth[offset++];
                Require(fields.Add(key), "Duplicate COSE key");
                if(key == 1 || key == 3 || key == 0x20)
                {
                    int expected = key == 1 ? 2 : key == 3 ? 0x26 : 1;
                    Require(offset < auth.Length && auth[offset++] == expected, "Unsupported COSE algorithm/curve/type");
                }
                else
                {
                    Require(key == 0x21 || key == 0x22, "Unknown COSE key");
                    Require(offset + 2 <= auth.Length && auth[offset++] == 0x58 && auth[offset++] == 32, "Invalid P-256 coordinate");
                    byte[] coordinate = Slice(auth, offset, 32); offset += 32;
                    if(key == 0x21) x = coordinate; else y = coordinate;
                }
            }
            Require(offset == auth.Length && fields.SetEquals(new int[]{1, 3, 0x20, 0x21, 0x22}) && x != null && y != null, "Incomplete/trailing COSE key");
            byte[] blob = new byte[72];
            System.Array.Copy(BitConverter.GetBytes(0x31534345), 0, blob, 0, 4); // BCRYPT_ECDSA_PUBLIC_P256_MAGIC
            System.Array.Copy(BitConverter.GetBytes(32), 0, blob, 4, 4);
            System.Array.Copy(x, 0, blob, 8, 32); System.Array.Copy(y, 0, blob, 40, 32);
            using(var key = CngKey.Import(blob, CngKeyBlobFormat.EccPublicBlob)) { }
            return blob;
        }
        internal static byte[] DerSignature(byte[] signature)
        {
            Require(signature.Length >= 8 && signature.Length <= 72 && signature[0] == 0x30
                && signature[1] == signature.Length - 2, "Invalid ES256 DER signature");
            int offset = 2; byte[] output = new byte[64];
            for(int index = 0; index < 2; index++)
            {
                Require(offset + 2 <= signature.Length && signature[offset++] == 2, "Missing DER integer");
                int length = signature[offset++];
                Require(length > 0 && length <= 33 && offset + length <= signature.Length && (signature[offset] & 0x80) == 0, "Invalid DER integer");
                if(length > 1 && signature[offset] == 0)
                {
                    Require((signature[offset + 1] & 0x80) != 0, "Nonminimal DER integer");
                    offset++; length--;
                }
                Require(length <= 32, "Oversized ES256 integer");
                System.Array.Copy(signature, offset, output, index * 32 + 32 - length, length); offset += length;
            }
            Require(offset == signature.Length, "Trailing DER bytes");
            return output;
        }
        internal static uint Verify(string rp, byte[] challenge, byte[] registeredId, byte[] publicKey,
            uint previousCounter, Proof proof)
        {
            Require(Equal(proof.CredentialId, registeredId) && Equal(proof.ClientData, ClientJson(rp, challenge, false)), "Credential or exact owner challenge mismatch");
            uint counter = Header(rp, proof.AuthenticatorData, false);
            Require(proof.AuthenticatorData.Length == 37, "Unexpected assertion extension data");
            Require((counter == 0 && previousCounter == 0) || counter > previousCounter, "Authenticator counter replay");
            byte[] signed = new byte[69]; System.Array.Copy(proof.AuthenticatorData, signed, 37);
            System.Array.Copy(Hash(proof.ClientData), 0, signed, 37, 32);
            Require(publicKey.Length == 72 && BitConverter.ToUInt32(publicKey, 0) == 0x31534345 && BitConverter.ToUInt32(publicKey, 4) == 32, "Unsupported public key");
            using(var key = CngKey.Import(publicKey, CngKeyBlobFormat.EccPublicBlob))
            using(var verifier = new ECDsaCng(key))
            {
                verifier.HashAlgorithm = CngAlgorithm.Sha256;
                Require(verifier.VerifyData(signed, DerSignature(proof.Signature)), "Windows credential signature did not verify");
            }
            // The owner MUST atomically consume its single-use challenge even on
            // authenticators with a zero counter; this method alone is not admission.
            return counter;
        }
    }
}

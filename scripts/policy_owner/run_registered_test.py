"""Prepare, then human-operate one registered successor TEST integration flow.

--prepare performs read-only owner queries and writes fresh external fixtures.
--run is reserved for the human's reviewed elevated action and Windows Hello.
It never provisions/enrolls an owner and never retries a failed operation.
"""

from __future__ import annotations

import argparse
import ctypes
import json
import os
import re
import subprocess
import sys
import traceback
from contextlib import contextmanager
from datetime import datetime, timezone
from hashlib import sha256
from pathlib import Path

SOURCE = Path(__file__).resolve().parents[2]
OWNER = Path(r"C:\ProgramData\TA-PolicyOwner-test-20260915-002")
DEPENDENCIES = Path(r"D:\prj\TradingAgents\TradingAgents\.venv\Lib\site-packages")
EVIDENCE = Path(r"D:\TA-PolicyOperationalIntegration-Evidence-20260914-001")
INSTANCE = "test-20260915-002"
EXPECTED = {
    "instance": INSTANCE,
    "storage_binding_identity": "policy-owner-storage-binding:v1:sha256:9cd6bbf08c31ff81838d14cd4d3b8861ba1afce3b3780380e0c6a7c62354b123",
    "ledger_identity": "policy-owner-durable-ledger-registration:v1:sha256:1205fd980d3f78a788e91e3ecf48a3c61c55e37fd501491630b5ff3bf59361e3",
    "native_sha256": "9abb15bf5b29d12d039715e9297643e03c9a14fbdf27d3ed979ef7bb923f6e91",
    "worker_manifest_sha256": "c8a22299fab71c598eaf66a42e9e0fc5ba2f2a66f8b455689101fde858b9f292",
}
AUTHENTICATION_ID = "policy-owner-authenticated-human-record:v1:sha256:c392b23758af2521fa951153ef9f0d39307a2d07943753e35c4747069f4f27da"
CONSUMER_SHA256 = "87785d37701d16aa6a409cc9ebbba6367fa8ca5d25fb6dccc7b317503e4efc3d"
POWERSHELL = Path(r"C:\Windows\System32\WindowsPowerShell\v1.0\powershell.exe")
CLIENT_PYTHON = Path(r"D:\Program Files\Python313\python.exe")
DEPLOYMENT_PATH = None
DEPLOYMENT_SHA256 = None
DEPLOYMENT_INPUTS = []
REVIEWED_CLIENT_IMAGES = []
NAMESPACE_BINDING = None
DEPLOYMENT_STAGING_ONLY = False


def canonical(value):
    return json.dumps(
        value, ensure_ascii=False, allow_nan=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")


def write_new(path, raw):
    with Path(path).open("xb") as stream:
        stream.write(raw)


def pin(path):
    raw = Path(path).read_bytes()
    return {
        "path": str(Path(path).resolve()),
        "byte_length": len(raw),
        "sha256": sha256(raw).hexdigest(),
    }


def read_exact_json(path, expected):
    """Read one explicitly reviewed input; never adopt its current bytes as a pin."""
    raw = Path(path).read_bytes()
    actual = {
        "path": str(Path(path).resolve()),
        "byte_length": len(raw),
        "sha256": sha256(raw).hexdigest(),
    }
    if actual != expected:
        raise ValueError("reviewed deployment input changed: " + str(path))

    def unique(pairs):
        value = {}
        for key, item in pairs:
            if key in value:
                raise ValueError("duplicate deployment JSON field")
            value[key] = item
        return value

    return json.loads(raw, object_pairs_hook=unique)


def configure_deployment(path, expected_sha, *, staging_only=False):
    """Select one byte-reviewed additive TEST setup; infer no future enrollment.

    The exact setup supplies immutable expected inputs. Storage object identities
    and the genuine enrollment are bound only after native status verifies them.
    This function never provisions, contacts an owner or reads its ledger.
    """
    global OWNER, DEPENDENCIES, INSTANCE, EXPECTED, AUTHENTICATION_ID, CONSUMER_SHA256
    global CLIENT_PYTHON, DEPLOYMENT_PATH, DEPLOYMENT_SHA256, DEPLOYMENT_INPUTS
    global REVIEWED_CLIENT_IMAGES
    global NAMESPACE_BINDING, DEPLOYMENT_STAGING_ONLY
    path = Path(path).resolve(strict=True)
    if not re.fullmatch(r"[0-9a-f]{64}", expected_sha or ""):
        raise ValueError("a reviewed deployment SHA-256 is required")
    initial = pin(path)
    initial["sha256"] = expected_sha
    deployment = read_exact_json(path, initial)
    fields = {
        "contract_version",
        "instance",
        "owner_root",
        "setup_proposal",
        "client_source",
        "client_python",
        "client_dependencies",
        "client_manifest",
        "reviewed_client_images",
        "operational",
        "execution_authorized",
    }
    if (
        set(deployment) != fields
        or deployment["contract_version"] != "registered-test-deployment-v1"
        or not re.fullmatch(r"test-[0-9]{8}-[0-9]{3}", deployment["instance"])
        or deployment["operational"] is not False
        or deployment["execution_authorized"] is not False
    ):
        raise ValueError("reviewed deployment must be a closed nonoperational TEST binding")
    instance = deployment["instance"]
    owner = Path(r"C:\ProgramData") / ("TA-PolicyOwner-" + instance)
    if (
        Path(deployment["owner_root"]) != owner
        or Path(deployment["client_source"]) != owner / "source"
        or Path(deployment["client_dependencies"]) != owner / "test-client-dependencies"
        or Path(deployment["client_python"]["path"]) != owner / "python/python.exe"
    ):
        raise ValueError("reviewed deployment source/root differs from this entrypoint")
    proposal_pin = deployment["setup_proposal"]
    proposal = read_exact_json(proposal_pin["path"], proposal_pin)
    if (
        proposal["contract_version"] != "policy-owner-setup-proposal-v1"
        or proposal["instance"] != instance
        or Path(proposal["root"]) != owner
        or proposal["execution_authorized"] is not False
        or proposal["registration_is_approval"] is not False
    ):
        raise ValueError("setup does not bind the exact TEST deployment")
    unsigned = {key: value for key, value in proposal.items() if key != "identity"}
    if (
        proposal["identity"]
        != "policy-owner-setup-proposal:v1:sha256:" + sha256(canonical(unsigned)).hexdigest()
    ):
        raise ValueError("setup proposal identity changed")
    files = {row["relative_path"]: row for row in proposal["files"]}
    if len(files) != len(proposal["files"]):
        raise ValueError("duplicate setup member")
    staged = Path(proposal_pin["path"]).resolve().parent / "depot"
    location = staged if staging_only else owner
    if location / "source" != SOURCE:
        raise ValueError("the human TEST driver must run from its protected installed source")
    staged_inputs = []
    for name, row in files.items():
        if name.startswith("state/"):
            # Only native setup may validate pristine control state. Enrollment
            # subsequently changes it; client preparation never reads a DB.
            continue
        member = location / name
        if (
            not member.resolve().is_relative_to(location)
            or member.is_symlink()
            or not member.is_file()
        ):
            raise ValueError("invalid reviewed setup member")
        expected = {
            "path": str(member.resolve()),
            "byte_length": row["byte_length"],
            "sha256": row["sha256"],
        }
        if pin(member) != expected:
            raise ValueError("reviewed setup member changed: " + name)
        staged_inputs.append(expected)
    for subtree in ("source", "python", "dependencies", "test-client-dependencies"):
        expected_names = {name for name in files if name.startswith(subtree + "/")}
        actual_names = set()
        for member in (location / subtree).rglob("*"):
            if member.is_symlink() or getattr(member.lstat(), "st_file_attributes", 0) & 0x400:
                raise ValueError("reviewed import tree contains a reparse point")
            if member.is_file():
                actual_names.add(member.relative_to(location).as_posix())
        if actual_names != expected_names:
            raise ValueError("reviewed import tree membership changed: " + subtree)
    client_manifest_pin = deployment["client_manifest"]
    client_manifest = read_exact_json(client_manifest_pin["path"], client_manifest_pin)
    if (
        set(client_manifest) != {"contract_version", "files", "execution_authorized"}
        or client_manifest["contract_version"] != "registered-test-client-files-v1"
        or client_manifest["execution_authorized"] is not False
    ):
        raise ValueError("unknown delivered client manifest")
    dependencies = location / "test-client-dependencies"

    def local_pin(row):
        declared = Path(row["path"])
        if not declared.is_relative_to(owner):
            raise ValueError("client import input is outside its protected TEST depot")
        return {**row, "path": str((location / declared.relative_to(owner)).resolve())}

    client_files = [local_pin(row) for row in client_manifest["files"]]
    expected_members = {row["path"] for row in client_files}
    observed_members = {
        str(member.resolve()) for member in dependencies.rglob("*") if member.is_file()
    }
    if (
        len(expected_members) != len(client_manifest["files"])
        or observed_members != expected_members
    ):
        raise ValueError("delivered client dependency membership changed")
    inputs = [
        initial,
        proposal_pin,
        client_manifest_pin,
        local_pin(deployment["client_python"]),
        *staged_inputs,
        *client_files,
        *deployment["reviewed_client_images"],
    ]
    for row in inputs:
        if pin(row["path"]) != row:
            raise ValueError("reviewed client input changed: " + str(row["path"]))
    EXPECTED = {
        "instance": instance,
        "operator_sid": proposal["operator_sid"],
        "ledger_identity": proposal["ledger_identity"],
        "native_sha256": files["bin/PolicyOwner.exe"]["sha256"],
        "worker_manifest_sha256": files["worker-manifest.json"]["sha256"],
        "setup_proposal_sha256": files["role-proposal.json"]["sha256"],
        "original_registry_sha256": files["original-acquisitions.json"]["sha256"],
    }
    INSTANCE, OWNER, DEPENDENCIES = instance, owner, dependencies
    CLIENT_PYTHON = location / "python/python.exe"
    CONSUMER_SHA256 = files["bin/ConditionalRuntimeProtectionHost.exe"]["sha256"]
    AUTHENTICATION_ID = None
    DEPLOYMENT_PATH, DEPLOYMENT_SHA256 = path, expected_sha
    DEPLOYMENT_INPUTS = inputs
    REVIEWED_CLIENT_IMAGES = deployment["reviewed_client_images"]
    NAMESPACE_BINDING = {
        "consumer": {
            "path": str(location / "bin/ConditionalRuntimeProtectionHost.exe"),
            **{
                key: files["bin/ConditionalRuntimeProtectionHost.exe"][key]
                for key in ("sha256", "byte_length")
            },
        },
        "library": {
            "path": str(location / "source/scripts/policy_owner/pinned_setup_native.ps1"),
            **{
                key: files["source/scripts/policy_owner/pinned_setup_native.ps1"][key]
                for key in ("sha256", "byte_length")
            },
        },
        "helper": {
            "path": str(location / "source/scripts/policy_owner/initialize_test_namespace.ps1"),
            **{
                key: files["source/scripts/policy_owner/initialize_test_namespace.ps1"][key]
                for key in ("sha256", "byte_length")
            },
        },
        "protection_source_identity": "runtime-protection-source:v1:sha256:"
        + files["source/scripts/runtime_protection/RuntimeProtectionHost.cs"]["sha256"],
        "fixture_root": str(staged.parent / "SYNTHETIC-namespace-fixtures"),
    }
    DEPLOYMENT_STAGING_ONLY = staging_only


def require_current_test_proposal(proposal, *, at=None):
    """Reject an obsolete unsigned action before any owner call or start marker."""
    from tradingagents.policy_coverage_models import read_model
    from tradingagents.policy_owner_contract import PolicyDecisionProposal
    from tradingagents.policy_qualified_contract import utc

    value = read_model(PolicyDecisionProposal, proposal)
    checked = datetime.now(timezone.utc) if at is None else at
    if checked.tzinfo is None or checked.utcoffset() != timezone.utc.utcoffset(checked):
        raise ValueError("the TEST action clock must be explicit UTC")
    if value.instance != INSTANCE:
        raise ValueError("the reviewed TEST proposal belongs to another instance")
    if not utc(value.execution.effective_utc) <= checked < utc(value.execution.expires_utc):
        raise ValueError(
            "reviewed TEST proposal is expired or not yet effective; preserve it and obtain an explicitly reviewed refresh"
        )
    return value


def read_retained_artifact(reference):
    """Read immutable preparation bytes; this is not an authority resolver.

    Native status must first validate the registered owner and authentication
    chain. Avoid worker RPC during preparation: even read-artifact constructs
    the worker owner, which advances its durable clock and reconciles records.
    """
    from tradingagents.policy_qualified_contract import EvidenceRef

    ref = EvidenceRef.read(reference)
    path = OWNER / "artifacts" / (ref.pin.sha256 + ".blob")
    status = path.lstat()
    if (
        not path.is_file()
        or path.is_symlink()
        or status.st_nlink != 1
        or getattr(status, "st_file_attributes", 0) & 0x400
    ):
        raise ValueError("retained preparation artifact must be a regular unlinked file")
    raw = path.read_bytes()
    ref.pin.check(raw)
    return raw


def require_deployed_source_consistency():
    """Do not prepare an action that conceals a proposed immutable source fix."""
    raw = (OWNER / "worker-manifest.json").read_bytes()
    if sha256(raw).hexdigest() != EXPECTED["worker_manifest_sha256"]:
        raise ValueError("registered worker manifest changed")
    manifest = json.loads(raw)
    mismatches = []
    for entry in manifest["files"]:
        name = entry["relative_path"]
        if not name.startswith("source/"):
            continue
        current = SOURCE / name.removeprefix("source/")
        if not current.is_file():
            mismatches.append(name)
            continue
        actual = pin(current)
        if actual["sha256"] != entry["sha256"] or actual["byte_length"] != entry["byte_length"]:
            mismatches.append(name)
    if mismatches:
        raise ValueError(
            "proposed source differs from the deployed immutable binding; preserve the instance and obtain a reviewed deployment decision: "
            + ", ".join(mismatches)
        )
    return manifest


def client_native_images(dependencies=None, reviewed_client_images=()):
    """Retain actual loaded client DLL origins, separate from owner-copy pins."""
    kernel = ctypes.WinDLL(r"C:\Windows\System32\kernel32.dll", use_last_error=True)
    psapi = ctypes.WinDLL(r"C:\Windows\System32\psapi.dll", use_last_error=True)
    kernel.GetCurrentProcess.restype = ctypes.c_void_p
    psapi.EnumProcessModulesEx.argtypes = [
        ctypes.c_void_p,
        ctypes.POINTER(ctypes.c_void_p),
        ctypes.c_uint32,
        ctypes.POINTER(ctypes.c_uint32),
        ctypes.c_uint32,
    ]
    psapi.EnumProcessModulesEx.restype = ctypes.c_int
    kernel.GetModuleFileNameW.argtypes = [ctypes.c_void_p, ctypes.c_wchar_p, ctypes.c_uint32]
    kernel.GetModuleFileNameW.restype = ctypes.c_uint32
    modules = (ctypes.c_void_p * 4096)()
    needed = ctypes.c_uint32()
    if not psapi.EnumProcessModulesEx(
        kernel.GetCurrentProcess(), modules, ctypes.sizeof(modules), ctypes.byref(needed), 3
    ) or needed.value > ctypes.sizeof(modules):
        raise ValueError("cannot close the actual client native module inventory")
    private, platform = set(), set()
    dependencies = DEPENDENCIES if dependencies is None else Path(dependencies).resolve()
    reviewed_images = {str(Path(row["path"]).resolve()): row for row in reviewed_client_images}
    if len(reviewed_images) != len(reviewed_client_images):
        raise ValueError("duplicate reviewed client native image")
    for name, expected in reviewed_images.items():
        if pin(name) != expected:
            raise ValueError("reviewed client native image changed: " + name)
    base = Path(sys.base_prefix).resolve()
    for module in modules[: needed.value // ctypes.sizeof(ctypes.c_void_p)]:
        path_buffer = ctypes.create_unicode_buffer(32768)
        length = kernel.GetModuleFileNameW(module, path_buffer, len(path_buffer))
        if not 0 < length < len(path_buffer):
            raise ValueError("actual client native module origin is unavailable")
        path = Path(path_buffer.value).resolve(strict=True)
        if path.is_relative_to(base) or path.is_relative_to(dependencies):
            private.add(str(path))
        elif str(path) in reviewed_images:
            # Explicit exact-byte client input, held by the human bootstrap.
            # This grants no platform/native-consumer admission to a candidate.
            private.add(str(path))
        elif any(
            path.is_relative_to(root)
            for root in (Path(r"C:\Windows\System32"), Path(r"C:\Windows\WinSxS"))
        ):
            platform.add(str(path))
        else:
            raise ValueError(
                "client loaded a native image outside the reviewed local runtime/dependencies and Windows platform: "
                + str(path)
            )
    if str(base / "python313.dll") not in private:
        raise ValueError("the current client is not using its bound adjacent Python DLL")
    # Include adjacent base images that a later import may select as well.
    private.update(str(path.resolve()) for path in base.glob("*.dll"))
    private.update(
        str(path.resolve())
        for path in (base / "DLLs").glob("*")
        if path.suffix.lower() in {".pyd", ".dll"}
    )
    return {
        "private_images": sorted(private),
        "windows_platform_images": sorted(platform),
        "reviewed_client_images": list(reviewed_images.values()),
        "platform_trust": "existing Windows System32/WinSxS platform; not owner payload",
    }


def write_human_action(root):
    """Create a byte-pinned, one-shot bootstrap; generating it executes nothing."""

    def literal(value):
        return "'" + str(value).replace("'", "''") + "'"

    package_path = root / "TEST-package.json"
    package_pin = pin(package_path)["sha256"]
    library = SOURCE / "scripts/policy_owner/pinned_setup_native.ps1"
    prefix_raw = library.read_bytes()
    expected_library = next(
        row
        for row in json.loads(package_path.read_bytes())["file_pins"]
        if Path(row["path"]) == library
    )
    if (
        len(prefix_raw) != expected_library["byte_length"]
        or sha256(prefix_raw).hexdigest() != expected_library["sha256"]
    ):
        raise ValueError("pin-library source changed while materializing the human action")
    prefix = prefix_raw.decode("utf-8")
    body = r"""
$ErrorActionPreference='Stop'
Microsoft.PowerShell.Core\Import-Module -Name ([IO.Path]::Combine($PSHOME,'Modules','Microsoft.PowerShell.Utility','Microsoft.PowerShell.Utility.psd1')) -ErrorAction Stop
Microsoft.PowerShell.Core\Import-Module -Name ([IO.Path]::Combine($PSHOME,'Modules','Microsoft.PowerShell.Management','Microsoft.PowerShell.Management.psd1')) -ErrorAction Stop
if ($PSVersionTable.PSEdition -ne 'Desktop' -or [IntPtr]::Size -ne 8) {throw 'Use 64-bit Windows PowerShell 5.1.'}
if ($PSVersionTable.PSVersion.Major -ne 5 -or $PSVersionTable.PSVersion.Minor -ne 1 -or [Threading.Thread]::CurrentThread.ApartmentState -ne 'STA') {throw 'Use Windows PowerShell 5.1 STA.'}
if ([Security.Principal.WindowsIdentity]::GetCurrent().User.Value -cne $operatorSid) {throw 'Use the exact reviewed issuing operator.'}
if (-not ([Security.Principal.WindowsPrincipal][Security.Principal.WindowsIdentity]::GetCurrent()).IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)) {throw 'Use the issuing operator''s elevated session for the genuine Windows Hello frontend.'}
$api=New-SetupPinApi
$pins=[Collections.Generic.List[IDisposable]]::new()
$heldDirectories=[Collections.Generic.HashSet[string]]::new([StringComparer]::OrdinalIgnoreCase)
function Read-HeldTestFile([string]$path,[string]$expected,[long]$length) {
    $parents=[Collections.Generic.List[string]]::new()
    $parent=[IO.DirectoryInfo]::new([IO.Path]::GetDirectoryName($path))
    while ($null -ne $parent) {$parents.Add($parent.FullName);$parent=$parent.Parent}
    for ($index=$parents.Count-1;$index -ge 0;$index--) {
        if ($heldDirectories.Add($parents[$index])) {$pins.Add((Open-SetupPin -Api $api -Path $parents[$index] -Directory $true))}
    }
    $leaf=Open-SetupPin -Api $api -Path $path -Directory $false
    $pins.Add($leaf)
    $stream=[IO.FileStream]::new($leaf,[IO.FileAccess]::Read)
    $pins.Add($stream)
    if ($length -ge 0 -and $stream.Length -ne $length) {throw ('Reviewed TEST length changed: '+$path)}
    $hash=[Security.Cryptography.SHA256]::Create()
    try {$actual=[BitConverter]::ToString($hash.ComputeHash($stream)).Replace('-','').ToLowerInvariant()}
    finally {$hash.Dispose()}
    if ($actual -cne $expected) {throw ('Reviewed TEST bytes changed: '+$path)}
    $stream.Position=0
    return $stream
}
try {
    $packageStream=Read-HeldTestFile $packagePath $packageSha256 -1
    $reader=[IO.StreamReader]::new($packageStream,[Text.Encoding]::UTF8,$true,4096,$true)
    try {$package=$reader.ReadToEnd() | ConvertFrom-Json} finally {$reader.Dispose()}
    if ($package.instance -cne $expectedInstance -or $package.operational -ne $false -or $package.execution_authorized -ne $false) {throw 'TEST package scope changed.'}
    foreach ($row in $package.file_pins) {$null=Read-HeldTestFile $row.path $row.sha256 ([long]$row.byte_length)}
    Write-Host 'Starting one TEST flow. Review each exact native document and authenticate yourself with Windows Hello. No provisioning, enrollment, ISO, Ticket-07 or live operation.'
    $runArguments=@('-I','-B','-S',$runner,'--run','--output',$outputRoot)
    if ($package.coverage_v2 -eq $true) {$runArguments+=@('--coverage-v2')}
    if ($null -ne $deploymentPath) {$runArguments+=@('--deployment',$deploymentPath,'--deployment-sha256',$deploymentSha256)}
    & $python @runArguments
    if ($LASTEXITCODE -ne 0) {throw ('TEST flow stopped with exit '+$LASTEXITCODE+'. Do not rerun this action; preserve its evidence.')}
}
finally {for ($index=$pins.Count-1;$index -ge 0;$index--) {$pins[$index].Dispose()}}
"""
    settings = "\n".join(
        (
            "$packagePath=" + literal(package_path),
            "$packageSha256=" + literal(package_pin),
            "$python=" + literal(sys.executable),
            "$runner=" + literal(Path(__file__).resolve()),
            "$outputRoot=" + literal(root),
            "$expectedInstance=" + literal(INSTANCE),
            "$operatorSid="
            + literal(EXPECTED.get("operator_sid", "S-1-5-21-3778926318-16405171-1220131840-1001")),
            "$deploymentPath=" + (literal(DEPLOYMENT_PATH) if DEPLOYMENT_PATH else "$null"),
            "$deploymentSha256=" + (literal(DEPLOYMENT_SHA256) if DEPLOYMENT_PATH else "$null"),
        )
    )
    action = root / "Invoke-RegisteredTest.ps1"
    write_new(action, (prefix + "\n" + settings + "\n" + body).encode("utf-8"))
    digest = pin(action)["sha256"]
    command = (
        "$testAction="
        + literal(action)
        + "; $testStream=[IO.File]::Open($testAction,'Open','Read','Read'); "
        "try {$testMemory=[IO.MemoryStream]::new(); $testStream.CopyTo($testMemory); $testBytes=$testMemory.ToArray(); "
        "$testMemory.Dispose(); $testHash=[Security.Cryptography.SHA256]::Create(); "
        "try {$testDigest=[BitConverter]::ToString($testHash.ComputeHash($testBytes)).Replace('-','').ToLowerInvariant()} "
        "finally {$testHash.Dispose()}; if($testDigest -cne "
        + literal(digest)
        + ") {throw 'Reviewed action bytes changed'}; "
        "& ([ScriptBlock]::Create([Text.Encoding]::UTF8.GetString($testBytes)))} finally {$testStream.Dispose()}"
    )
    write_new(root / "HUMAN-COMMAND.txt", (command + "\n").encode("utf-8"))
    return {"action": str(action), "sha256": digest, "command": str(root / "HUMAN-COMMAND.txt")}


def namespace_initializer_arguments(
    request_path=None, *, staged_fixture=False, request_sha256=None
):
    """The only permitted pre-authentication helper operation is validation."""
    if NAMESPACE_BINDING is None or DEPLOYMENT_PATH is None:
        raise ValueError("exact reviewed deployment is required for namespace operations")
    if staged_fixture != DEPLOYMENT_STAGING_ONLY:
        raise ValueError("staged namespace inputs cannot select registered execution")
    arguments = [
        POWERSHELL,
        "-NoProfile",
        "-NonInteractive",
        "-File",
        NAMESPACE_BINDING["helper"]["path"],
        "-Instance",
        INSTANCE,
        "-DeploymentPath",
        DEPLOYMENT_PATH,
        "-DeploymentSha256",
        DEPLOYMENT_SHA256,
        "-ConsumerPath",
        NAMESPACE_BINDING["consumer"]["path"],
        "-ConsumerSha256",
        NAMESPACE_BINDING["consumer"]["sha256"],
        "-PinLibraryPath",
        NAMESPACE_BINDING["library"]["path"],
        "-PinLibrarySha256",
        NAMESPACE_BINDING["library"]["sha256"],
    ]
    if staged_fixture:
        arguments.append("-StagedFixture")
    if request_path is None:
        arguments.append("-ValidateOnly")
    else:
        arguments += [
            "-RequestPath",
            request_path,
            "-RequestSha256",
            request_sha256 if request_sha256 is not None else pin(request_path)["sha256"],
        ]
    return arguments


def validate_namespace_initializer(client, *, staged_fixture=False):
    checked = client.execute(
        namespace_initializer_arguments(staged_fixture=staged_fixture),
        label="namespace-initializer-preflight",
    )
    expected = {
        "contract_version": "registered-test-namespace-preflight-v1",
        "instance": INSTANCE,
        "deployment_sha256": DEPLOYMENT_SHA256,
        "consumer_path": NAMESPACE_BINDING["consumer"]["path"],
        "consumer_sha256": NAMESPACE_BINDING["consumer"]["sha256"],
        "library_sha256": NAMESPACE_BINDING["library"]["sha256"],
        "helper_sha256": NAMESPACE_BINDING["helper"]["sha256"],
        "protection_source_identity": NAMESPACE_BINDING["protection_source_identity"],
        "staged_fixture": staged_fixture,
        "native_entrypoint_loaded": True,
        "namespace_initialized": False,
        "operational": False,
        "execution_authorized": False,
    }
    if canonical(checked) != canonical(expected):
        raise ValueError("namespace initializer preflight lost the exact TEST binding")


class BoundedClient:
    """Limit this Python driver; native trusted boundaries enforce their own ACLs."""

    def __init__(self, root, run):
        self.root = root
        self.run = run
        self.denied = []
        self.read_files = set()
        self.sequence = 0
        self.executables = {
            str(OWNER / "bin/PolicyOwner.exe").casefold(),
            str(POWERSHELL).casefold(),
        }
        if run:
            self.executables.update(
                (
                    str(OWNER / "bin/ConditionalRuntimeProtectionHost.exe").casefold(),
                    str(POWERSHELL).casefold(),
                )
            )
        self.parse_command_line = ctypes.windll.shell32.CommandLineToArgvW
        self.parse_command_line.argtypes = [ctypes.c_wchar_p, ctypes.POINTER(ctypes.c_int)]
        self.parse_command_line.restype = ctypes.POINTER(ctypes.c_wchar_p)
        self.local_free = ctypes.windll.kernel32.LocalFree
        self.local_free.argtypes = [ctypes.c_void_p]
        self.local_free.restype = ctypes.c_void_p

    def local_write(self, value):
        if isinstance(value, int):
            return
        path = Path(value).resolve(strict=False)
        if not path.is_relative_to(self.root):
            raise ValueError(
                "TEST client attempted a write outside its fresh evidence root: " + str(path)
            )

    def audit(self, event, args):
        if event.startswith("socket.") and event not in {"socket.__new__", "socket.gethostname"}:
            self.denied.append(event)
            raise ValueError("TEST client blocks network operations: " + event)
        if event in {"sqlite3.connect", "os.system", "os.spawn", "os.exec"}:
            self.denied.append(event)
            raise ValueError("TEST client blocks provider/database/shell workload: " + event)
        if event == "subprocess.Popen":
            count = ctypes.c_int()
            parsed = self.parse_command_line(args[1], ctypes.byref(count))
            if not parsed or count.value < 2:
                raise ValueError("invalid reviewed Windows subprocess command line")
            try:
                arguments = [parsed[i] for i in range(count.value)]
            finally:
                self.local_free(parsed)
            executable = str(Path(args[0] or arguments[0]).resolve(strict=True)).casefold()
            if executable not in self.executables:
                self.denied.append(event)
                raise ValueError("TEST client refuses an unreviewed child executable")
            if not self.run:
                status_arguments = [str(OWNER / "bin/PolicyOwner.exe"), "status", INSTANCE]
                validation_arguments = (
                    [str(value) for value in namespace_initializer_arguments()]
                    if NAMESPACE_BINDING is not None and not DEPLOYMENT_STAGING_ONLY
                    else None
                )
                if arguments != status_arguments and arguments != validation_arguments:
                    raise ValueError(
                        "preparation permits only exact read-only native status or namespace ValidateOnly"
                    )
        if event == "open":
            mode, flags = args[1], args[2]
            writing = (isinstance(mode, str) and any(c in mode for c in "wax+")) or (
                isinstance(flags, int)
                and flags & (os.O_WRONLY | os.O_RDWR | os.O_CREAT | os.O_TRUNC | os.O_APPEND)
            )
            if writing:
                self.local_write(args[0])
            elif not isinstance(args[0], int):
                self.read_files.add(str(Path(args[0]).resolve(strict=False)))
        if event in {"os.mkdir", "os.remove", "os.rmdir", "os.chmod", "os.utime"}:
            self.local_write(args[0])
        if event in {"os.rename", "os.link", "os.symlink"}:
            self.local_write(args[0])
            self.local_write(args[1])

    def execute(self, args, *, label, human=False):
        self.sequence += 1
        prefix = self.root / (f"action-{self.sequence:03d}-" + label)
        write_new(
            prefix.with_suffix(".request.json"),
            canonical(
                {
                    "arguments": [str(a) for a in args],
                    "human_frontend": human,
                    "automatic_retry_permitted": False,
                    "operational": False,
                    "execution_authorized": False,
                }
            ),
        )
        try:
            completed = subprocess.run(
                [str(a) for a in args],
                capture_output=True,
                check=False,
                timeout=3600 if human else 600,
                creationflags=subprocess.CREATE_NO_WINDOW,
            )
        except subprocess.TimeoutExpired as error:
            write_new(prefix.with_suffix(".stdout"), error.stdout or b"")
            write_new(prefix.with_suffix(".stderr"), error.stderr or b"")
            write_new(prefix.with_suffix(".exit.json"), canonical({"status": "TIMEOUT_NO_RETRY"}))
            raise
        write_new(prefix.with_suffix(".stdout"), completed.stdout)
        write_new(prefix.with_suffix(".stderr"), completed.stderr)
        write_new(prefix.with_suffix(".exit.json"), canonical({"returncode": completed.returncode}))
        if completed.returncode:
            raise ValueError(
                "TEST action failed and was not retried; retained evidence: " + str(prefix)
            )
        from tradingagents.policy_qualified_contract import strict_json

        return strict_json(completed.stdout)

    def authenticate(self, operation, document):
        if not self.run or operation not in {"register-evidence", "decide"}:
            raise ValueError("human action is unavailable in prepare mode")
        raw = canonical(document)
        path = self.root / (f"human-{self.sequence + 1:03d}-" + operation + ".json")
        write_new(path, raw)
        print(
            "Genuine human review and Windows Hello required for TEST "
            + operation
            + ": "
            + str(path),
            file=sys.stderr,
            flush=True,
        )
        return self.execute(
            [OWNER / "bin/PolicyOwner.exe", operation, INSTANCE, path, sha256(raw).hexdigest()],
            label=operation,
            human=True,
        )

    def native(self, operation, document, *, label, control=None):
        if not self.run or operation not in {"verify", "prepare", "run-inert"}:
            raise ValueError("native fixture operation is outside the human run")
        raw = canonical(document)
        path = self.root / (label + ".json")
        write_new(path, raw)
        args = [
            OWNER / "bin/ConditionalRuntimeProtectionHost.exe",
            operation,
            INSTANCE,
            path,
            sha256(raw).hexdigest(),
        ]
        if control is not None:
            raw_control = canonical(control)
            control_path = self.root / (label + "-control.json")
            write_new(control_path, raw_control)
            args += [control_path, sha256(raw_control).hexdigest()]
        return self.execute(args, label=label)


def anchor_and_enrollment(client):
    global AUTHENTICATION_ID
    import winreg

    from tradingagents.policy_owner_contract import (
        OriginalAcquisitionRegistry,
        RoleProposal,
        StorageBinding,
    )
    from tradingagents.policy_qualified_contract import BytePin, EvidenceRef, strict_json

    with winreg.OpenKey(
        winreg.HKEY_LOCAL_MACHINE,
        "SOFTWARE\\TradingAgents\\PolicyOwner\\" + INSTANCE,
        0,
        winreg.KEY_READ | winreg.KEY_WOW64_64KEY,
    ) as key:
        anchor = strict_json(winreg.QueryValueEx(key, "AnchorJson")[0])
    if (
        any(anchor.get(key) != value for key, value in EXPECTED.items())
        or anchor["operational"] is not False
    ):
        raise ValueError("actual registered owner differs from the reviewed successor")
    if (
        Path(anchor["root"]) != OWNER
        or Path(anchor["native_path"]) != OWNER / "bin/PolicyOwner.exe"
    ):
        raise ValueError("actual registered paths differ from the successor")
    for path, digest in (
        (OWNER / "worker-manifest.json", EXPECTED["worker_manifest_sha256"]),
        (OWNER / "bin/PolicyOwner.exe", EXPECTED["native_sha256"]),
        (OWNER / "bin/ConditionalRuntimeProtectionHost.exe", CONSUMER_SHA256),
    ):
        if pin(path)["sha256"] != digest:
            raise ValueError("immutable registered file drift: " + str(path))
    require_deployed_source_consistency()
    status = client.execute(
        [OWNER / "bin/PolicyOwner.exe", "status", INSTANCE], label="registered-status"
    )
    if (
        status.get("enrolled") is not True
        or status.get("operational") is not False
        or status.get("execution_authorized") is not False
        or status.get("instance") != INSTANCE
        or status.get("storage_binding_identity") != anchor["storage_binding_identity"]
    ):
        raise ValueError("registered successor is not enrolled in nonoperational TEST state")
    storage = StorageBinding.read((OWNER / "storage-binding.json").read_bytes())
    role = RoleProposal.read((OWNER / "role-proposal.json").read_bytes())
    registry = OriginalAcquisitionRegistry.read((OWNER / "original-acquisitions.json").read_bytes())
    if (
        storage.identity != anchor["storage_binding_identity"]
        or (
            "storage_binding_identity" in EXPECTED
            and storage.identity != EXPECTED["storage_binding_identity"]
        )
        or role.instance != INSTANCE
        or role.operator_sid != anchor["operator_sid"]
    ):
        raise ValueError("enrollment/storage/principal linkage drift")
    records = sorted((OWNER / "authentication").glob("*.json"))
    if len(records) != 1:
        raise ValueError(
            "fresh TEST preparation requires exactly the retained enrollment record; partial or later authenticated state must be investigated"
        )
    record = strict_json(records[0].read_bytes())
    if (
        (AUTHENTICATION_ID is not None and record["identity"] != AUTHENTICATION_ID)
        or record["purpose"] != "ENROLL_PROJECT_ROLE"
        or record["document"] != role.model_dump(mode="json")
        or record["instance"] != INSTANCE
        or record["operator_sid"] != anchor["operator_sid"]
        or record["storage_binding_identity"] != storage.identity
    ):
        raise ValueError("retained authentication is not the verified successor enrollment")
    AUTHENTICATION_ID = record["identity"]
    EXPECTED["storage_binding_identity"] = storage.identity
    raw = canonical(record)
    ref = EvidenceRef(identity=AUTHENTICATION_ID, pin=BytePin.of(raw))
    if read_retained_artifact(ref) != raw:
        raise ValueError("retained enrollment artifact differs from the native-validated record")
    return anchor, role, registry


def prepare_or_run(args):
    global AUTHENTICATION_ID
    if DEPLOYMENT_PATH is None or DEPLOYMENT_STAGING_ONLY:
        raise ValueError("registered preparation/run requires the exact installed deployment")
    if not (sys.flags.isolated and sys.flags.no_site and sys.dont_write_bytecode):
        raise ValueError("use the reviewed existing interpreter with -I -B -S")
    if Path(sys.executable).resolve() != CLIENT_PYTHON.resolve():
        raise ValueError(
            "this client exercise uses the explicitly approved existing Python 3.13 runtime"
        )
    root = args.output.resolve(strict=False)
    if root.parent != EVIDENCE or not re.fullmatch(
        r"registered-human-test-[0-9]{8}-[0-9]{3}", root.name
    ):
        raise ValueError(
            "use a fresh registered-human-test-* child of the authorized external evidence directory"
        )
    if args.prepare:
        root.mkdir(exist_ok=False)
    elif not root.is_dir():
        raise ValueError("prepare the exact reviewable TEST package first")
    os.environ.clear()
    os.environ.update(
        SystemRoot=r"C:\Windows",
        SystemDrive="C:",
        WINDIR=r"C:\Windows",
        PATH=r"C:\Windows\System32",
        PATHEXT=".COM;.EXE;.BAT;.CMD",
        PYTHON_DOTENV_DISABLED="1",
        TRADINGAGENTS_OUTPUT_LANGUAGE="English",
        TRADINGAGENTS_CLI_ANNOUNCEMENTS_ENABLED="0",
        TUSHARE_TOKEN="test-token",
    )
    sys.path[:0] = [str(SOURCE), str(DEPENDENCIES)]
    client = BoundedClient(root, args.run)
    sys.addaudithook(client.audit)
    from scripts.policy_owner.registered_test_client import build_test_blueprint
    from scripts.policy_owner.registered_test_flow import RegisteredTestCase
    from scripts.policy_owner.registered_test_native import collect_payload, exercise_native
    from tradingagents.policy_qualified_contract import strict_json
    from tradingagents.windows_policy_owner import RegisteredWindowsPolicyVerifier

    class RetainedRegisteredVerifier(RegisteredWindowsPolicyVerifier):
        def call(self, operation, payload):
            client.sequence += 1
            prefix = root / (f"rpc-{client.sequence:03d}-" + operation)
            write_new(
                prefix.with_suffix(".request.json"),
                canonical({"operation": operation, "payload": payload}),
            )
            try:
                response = super().call(operation, payload)
            except Exception:
                write_new(
                    prefix.with_suffix(".failure.txt"), traceback.format_exc().encode("utf-8")
                )
                raise
            write_new(prefix.with_suffix(".response.json"), canonical(response))
            return response

    if args.run:
        proposal = require_current_test_proposal(
            (root / "TEST-decision-proposal.json").read_bytes()
        )
        if not ctypes.windll.shell32.IsUserAnAdmin():
            raise ValueError(
                "the genuine human frontend requires the issuing operator's elevated session"
            )
        package = strict_json((root / "TEST-package.json").read_bytes())
        if bool(package.get("coverage_v2", False)) != args.coverage_v2:
            raise ValueError("TEST action coverage version differs from reviewed package")
        if DEPLOYMENT_PATH is not None:
            if (
                package.get("deployment_sha256") != DEPLOYMENT_SHA256
                or set(package["expected_owner"]) != set(EXPECTED) | {"storage_binding_identity"}
                or any(
                    package["expected_owner"].get(key) != value for key, value in EXPECTED.items()
                )
            ):
                raise ValueError("TEST package differs from the reviewed deployment")
            EXPECTED["storage_binding_identity"] = package["expected_owner"][
                "storage_binding_identity"
            ]
            AUTHENTICATION_ID = package["enrollment_identity"]
        for row in package["file_pins"]:
            if pin(row["path"]) != row:
                raise ValueError("reviewed TEST input bytes changed: " + row["path"])
        client.sequence = package["last_action_sequence"]
        write_new(
            root / "HUMAN-RUN-STARTED.json",
            canonical({"no_retry": True, "operational": False, "execution_authorized": False}),
        )
    try:
        anchor, role, registry = anchor_and_enrollment(client)
        validate_namespace_initializer(client)
        if args.prepare:
            registry_path = root / "SYNTHETIC-instrument-registry.json"
            write_new(
                registry_path,
                canonical(
                    {
                        "schema_version": "1.0",
                        "registry_id": "synthetic-inert-client-registry-v1",
                        "rows": [
                            {
                                "canonical_symbol": "601328.SS",
                                "aliases": [],
                                "venue": "XSHG",
                                "instrument_kind": "equity",
                                "currency": "CNY",
                                "display_name": "SYNTHETIC offline configuration fixture only",
                                "provenance": {
                                    "provider": "SYNTHETIC offline fixture",
                                    "source_ref": "fixture:synthetic-instrument-no-acquisition",
                                    "retrieved_at": "2026-09-01T00:00:00Z",
                                },
                            }
                        ],
                    }
                ),
            )
        else:
            registry_path = root / "SYNTHETIC-instrument-registry.json"
        blueprint, _config = build_test_blueprint(
            root / "SYNTHETIC-client-configuration-only", registry_path
        )
        case = RegisteredTestCase(
            root=root,
            instance=INSTANCE,
            role=role,
            storage_identity=anchor["storage_binding_identity"],
            registry=registry,
            authenticate=client.authenticate,
            verifier=RetainedRegisteredVerifier(INSTANCE),
            artifact_reader=read_retained_artifact if args.prepare else None,
            coverage_v2=args.coverage_v2,
        )
        if args.prepare:
            proposal = case.proposal()
            write_new(
                root / "TEST-decision-proposal.json", canonical(proposal.model_dump(mode="json"))
            )
            write_new(root / "TEST-policy.json", case.policy.to_bytes())
            payload = collect_payload(
                owner_root=OWNER,
                manifest=strict_json((OWNER / "worker-manifest.json").read_bytes()),
                fixture_root=root / "fixture-inputs",
                child_source=SOURCE / "scripts/policy_owner/registered_test_child.py",
            )
            write_new(root / "TEST-payload.json", canonical(payload))
            write_new(root / "TEST-blueprint.json", canonical(blueprint.model_dump(mode="json")))
            native_images = client_native_images(reviewed_client_images=REVIEWED_CLIENT_IMAGES)
            write_new(root / "CLIENT-NATIVE-IMAGES.json", canonical(native_images))
            pins = {
                str(Path(module.__file__).resolve())
                for module in tuple(sys.modules.values())
                if getattr(module, "__file__", None) and Path(module.__file__).is_file()
            }
            pins.update(
                str(Path(module.__cached__).resolve())
                for module in tuple(sys.modules.values())
                if getattr(module, "__cached__", None) and Path(module.__cached__).is_file()
            )
            pins.update(path for path in tuple(client.read_files) if Path(path).is_file())
            pins.update(native_images["private_images"])
            pins.update(
                str(OWNER / name)
                for name in (
                    "bin/PolicyOwner.exe",
                    "bin/ConditionalRuntimeProtectionHost.exe",
                    "worker-manifest.json",
                )
            )
            pins.add(str(root / "CLIENT-NATIVE-IMAGES.json"))
            pins.update(
                str(path)
                for path in (SOURCE / "scripts/policy_owner").glob("*")
                if path.suffix in {".py", ".ps1"}
            )
            pins.update(payload["locations"].values())
            pins.update(str(path) for path in root.glob("TEST-*.json"))
            pins.add(str(registry_path))
            pins.add(str(Path(sys.executable)))
            pins.update(row["path"] for row in DEPLOYMENT_INPUTS)
            package = {
                "label": "UNEXECUTED HUMAN-OPERATED REGISTERED TEST EXERCISE; NOT OPERATIONAL APPROVAL",
                "instance": INSTANCE,
                "expected_owner": EXPECTED,
                "enrollment_identity": AUTHENTICATION_ID,
                "deployment_sha256": DEPLOYMENT_SHA256,
                "source": str(SOURCE),
                "dependencies": str(DEPENDENCIES),
                "file_pins": [pin(path) for path in sorted(pins)],
                "test_decision_identity": proposal.identity,
                "test_policy_identity": case.policy.acceptance_policy_identity,
                "coverage_v2": args.coverage_v2,
                "operational": False,
                "execution_authorized": False,
                "no_automatic_retry": True,
                "denied_preflight_events": client.denied,
                "last_action_sequence": client.sequence,
            }
            write_new(root / "TEST-package.json", canonical(package))
            action = write_human_action(root)
            return {
                "status": "PREPARED_UNEXECUTED",
                "output": str(root),
                "package_sha256": pin(root / "TEST-package.json")["sha256"],
                "file_count": len(pins),
                "windows_hello_used_or_simulated": False,
                "execution_authorized": False,
                "human_action": action,
            }
        proposal = require_current_test_proposal(proposal)
        # Rebuild every policy/role/package field; retain only the fixed period
        # displayed in the reviewed proposal. No unvalidated model_copy values.
        if proposal.model_dump(
            mode="json", exclude={"identity", "execution"}
        ) != case.proposal().model_dump(mode="json", exclude={"identity", "execution"}):
            raise ValueError("reviewed TEST policy/role/package no longer matches")
        payload = strict_json((root / "TEST-payload.json").read_bytes())
        if (
            canonical(blueprint.model_dump(mode="json"))
            != (root / "TEST-blueprint.json").read_bytes()
        ):
            raise ValueError("actual CLI blueprint differs from the reviewed TEST configuration")
        case.start(proposal)
        result, envelope = case.finish()
        native_source = strict_json((OWNER / "native-consumer-source.json").read_bytes())
        source_pin = pin(OWNER / "source/scripts/runtime_protection/RuntimeProtectionHost.cs")
        namespace_request = {
            "contract_version": "runtime-protection-namespace-request-v1",
            "namespace_root": str(root / "SYNTHETIC-native-namespace"),
            "protection_source_identity": "runtime-protection-source:v1:sha256:"
            + source_pin["sha256"],
        }
        request_path = root / "TEST-namespace-request.json"
        write_new(request_path, canonical(namespace_request))
        namespace = client.execute(
            namespace_initializer_arguments(request_path),
            label="initialize-disposable-namespace",
        )
        binding = {
            "contract_version": "policy-qualified-native-owner-binding-v1",
            "instance": INSTANCE,
            "storage_binding_identity": anchor["storage_binding_identity"],
            "native_owner_sha256": anchor["native_sha256"],
            "worker_manifest_sha256": anchor["worker_manifest_sha256"],
            "native_consumer_source_identity": native_source["identity"],
            "execution_authorized": False,
        }
        report = exercise_native(
            case=case,
            result=result,
            envelope=envelope,
            blueprint=blueprint,
            payload=payload,
            namespace=namespace,
            owner_binding=binding,
            native_call=client.native,
            artifact_root=root / "native-results",
        )
        write_new(root / "TEST-RESULTS.json", canonical(report))
        return {"status": "TEST_FLOW_COMPLETED", "output": str(root), "execution_authorized": False}
    except Exception:
        failure = {
            "status": "FAILED_NO_AUTOMATIC_RETRY",
            "traceback": traceback.format_exc(),
            "denied_events": client.denied,
            "operational": False,
            "execution_authorized": False,
        }
        write_new(
            root / ("HUMAN-RUN-FAILED.json" if args.run else "PREPARATION-FAILED.json"),
            canonical(failure),
        )
        raise


@contextmanager
def hold_rehearsal_output(fixture_root, output):
    """Reject redirection before writes and hold every directory through use."""
    fixture_root = Path(os.path.abspath(fixture_root))
    output = Path(os.path.abspath(output))
    if not output.is_relative_to(fixture_root) or output == fixture_root:
        raise ValueError("rehearsal output must be inside its exact fixture root")
    kernel = ctypes.WinDLL(r"C:\Windows\System32\kernel32.dll", use_last_error=True)
    kernel.CreateFileW.argtypes = [
        ctypes.c_wchar_p,
        ctypes.c_uint32,
        ctypes.c_uint32,
        ctypes.c_void_p,
        ctypes.c_uint32,
        ctypes.c_uint32,
        ctypes.c_void_p,
    ]
    kernel.CreateFileW.restype = ctypes.c_void_p
    kernel.GetFileInformationByHandle.argtypes = [ctypes.c_void_p, ctypes.c_void_p]
    kernel.CloseHandle.argtypes = [ctypes.c_void_p]
    handles = []

    def hold(path):
        handle = kernel.CreateFileW(str(path), 0x80, 3, None, 3, 0x02200000, None)
        if handle in (None, ctypes.c_void_p(-1).value):
            raise ctypes.WinError(ctypes.get_last_error())
        handles.append(handle)
        information = ctypes.create_string_buffer(52)
        if not kernel.GetFileInformationByHandle(handle, information):
            raise ctypes.WinError(ctypes.get_last_error())
        attributes = ctypes.c_uint32.from_buffer(information).value
        if attributes & 0x400 or not attributes & 0x10:
            raise ValueError("rehearsal directory is redirected or not a directory")

    try:
        for parent in reversed(fixture_root.parents):
            hold(parent)
        current = fixture_root
        for component in (None, *output.relative_to(fixture_root).parts):
            if component is not None:
                current = current / component
            try:
                current.mkdir()
            except FileExistsError:
                if current == output:
                    raise
            hold(current)
        yield output, fixture_root
    finally:
        for handle in reversed(handles):
            kernel.CloseHandle(handle)


def rehearse_namespace(args):
    """Actual coordinator/wrapper/native path, confined to a staged TEST fixture."""
    if not DEPLOYMENT_STAGING_ONLY or NAMESPACE_BINDING is None:
        raise ValueError("namespace rehearsal requires the reviewed staged deployment")
    with hold_rehearsal_output(NAMESPACE_BINDING["fixture_root"], args.output) as (
        output,
        fixture_root,
    ):
        return _rehearse_namespace(args, output, fixture_root)


def _rehearse_namespace(args, output, fixture_root):
    environment = {
        "SystemRoot": r"C:\Windows",
        "SystemDrive": "C:",
        "WINDIR": r"C:\Windows",
        "PATH": r"C:\Windows\System32",
        "PATHEXT": ".COM;.EXE;.BAT;.CMD",
    }

    class StagedClient:
        def execute(self, arguments, *, label):
            arguments = [str(value) for value in arguments]
            write_new(
                output / (label + ".request.json"),
                canonical(
                    {
                        "arguments": arguments,
                        "fixture_only": True,
                        "owner_contacted": False,
                        "execution_authorized": False,
                    }
                ),
            )
            completed = subprocess.run(
                arguments,
                capture_output=True,
                check=False,
                timeout=60,
                env=environment,
                creationflags=subprocess.CREATE_NO_WINDOW,
                cwd=output,
            )
            write_new(output / (label + ".stdout"), completed.stdout)
            write_new(output / (label + ".stderr"), completed.stderr)
            write_new(
                output / (label + ".exit.json"), canonical({"returncode": completed.returncode})
            )
            if completed.returncode:
                raise ValueError("staged namespace helper rejected; retained " + label)
            return json.loads(completed.stdout)

    client = StagedClient()
    validate_namespace_initializer(client, staged_fixture=True)
    result = {
        "status": "STAGED_NAMESPACE_PREFLIGHT_VERIFIED",
        "instance": INSTANCE,
        "fixture_only": True,
        "owner_contacted": False,
        "authentication_executed": False,
        "operational": False,
        "execution_authorized": False,
    }
    if args.namespace_request is not None:
        request_path = Path(os.path.abspath(args.namespace_request))
        if not request_path.is_relative_to(fixture_root):
            raise ValueError("namespace rehearsal request escaped its exact fixture root")
        # This source is part of the already verified staged deployment.
        sys.path.insert(0, str(SOURCE))
        from scripts.policy_owner.build_policy_owner import hold_build_inputs

        with hold_build_inputs([request_path]):
            receipt = _initialize_rehearsal_namespace(client, args, request_path, fixture_root)
        result.update(status="STAGED_NAMESPACE_INITIALIZED", namespace=receipt)
    return result


def _initialize_rehearsal_namespace(client, args, request_path, fixture_root):
    request = read_exact_json(
        request_path,
        {
            "path": str(request_path),
            "byte_length": request_path.stat().st_size,
            "sha256": args.namespace_request_sha256,
        },
    )
    namespace = Path(request["namespace_root"]).resolve(strict=False)
    if (
        not request_path.is_relative_to(fixture_root)
        or not namespace.is_relative_to(fixture_root)
        or namespace == fixture_root
    ):
        raise ValueError("namespace rehearsal cannot reach an action or installed root")
    if request["protection_source_identity"] != NAMESPACE_BINDING["protection_source_identity"]:
        raise ValueError("namespace rehearsal source binding mismatch")
    receipt = client.execute(
        namespace_initializer_arguments(
            request_path, staged_fixture=True, request_sha256=args.namespace_request_sha256
        ),
        label="initialize-disposable-namespace",
    )
    if (
        receipt["namespace_root"] != str(namespace)
        or receipt["protection_source_identity"] != request["protection_source_identity"]
    ):
        raise ValueError("native namespace receipt lost its exact fixture binding")
    return receipt


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--prepare", action="store_true")
    mode.add_argument("--run", action="store_true")
    mode.add_argument("--check-deployment", action="store_true")
    mode.add_argument("--rehearse-namespace", action="store_true")
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--deployment", type=Path, required=True)
    parser.add_argument("--deployment-sha256", required=True)
    parser.add_argument(
        "--coverage-v2",
        action="store_true",
        help="Explicit versioned generated coverage TEST flow; requires its matching setup corpus",
    )
    parser.add_argument("--namespace-request", type=Path)
    parser.add_argument("--namespace-request-sha256")
    args = parser.parse_args()
    if bool(args.deployment) != bool(args.deployment_sha256):
        parser.error("--deployment and --deployment-sha256 are required together")
    if args.deployment is not None:
        configure_deployment(
            args.deployment,
            args.deployment_sha256,
            staging_only=args.check_deployment or args.rehearse_namespace,
        )
    if bool(args.namespace_request) != bool(args.namespace_request_sha256):
        parser.error("namespace request path and hash are required together")
    if args.namespace_request is not None and not args.rehearse_namespace:
        parser.error("namespace fixture requests are available only in rehearsal mode")
    if args.rehearse_namespace:
        if not (sys.flags.isolated and sys.flags.no_site and sys.dont_write_bytecode):
            raise ValueError("namespace rehearsal requires -I -B -S")
        if Path(sys.executable).resolve() != CLIENT_PYTHON.resolve():
            raise ValueError("use the exact staged interpreter")
        print(json.dumps(rehearse_namespace(args)))
        return 0
    if args.check_deployment:
        if args.deployment is None:
            parser.error("--check-deployment requires the exact reviewed deployment")
        if not (sys.flags.isolated and sys.flags.no_site and sys.dont_write_bytecode):
            raise ValueError("staged deployment check requires -I -B -S")
        if Path(sys.executable).resolve() != CLIENT_PYTHON.resolve():
            raise ValueError("use the exact staged interpreter")
        print(
            json.dumps(
                {
                    "status": "STAGED_DEPLOYMENT_INPUTS_VERIFIED",
                    "instance": INSTANCE,
                    "input_count": len(DEPLOYMENT_INPUTS),
                    "owner_contacted": False,
                    "authentication_executed": False,
                    "operational": False,
                    "execution_authorized": False,
                }
            )
        )
        return 0
    print(json.dumps(prepare_or_run(args)))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

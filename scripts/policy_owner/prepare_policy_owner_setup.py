"""Prepare a reviewable local setup bundle; never provision or enroll a user.

Uses only explicitly supplied existing interpreter/dependency/build inputs. The
result must be reviewed and run through the native visible setup by the operator.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sqlite3
import subprocess
import sys
from pathlib import Path


def initialize_owner_state(path: Path, instance: str, ledger_id: str) -> None:
    descriptor = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    os.close(descriptor)
    connection = sqlite3.connect(path)
    try:
        connection.execute("PRAGMA synchronous=FULL")
        connection.executescript("""
            CREATE TABLE metadata (
                instance TEXT NOT NULL, ledger_id TEXT NOT NULL, last_clock TEXT NOT NULL
            );
            CREATE TABLE artifacts (identity TEXT PRIMARY KEY, reference BLOB NOT NULL);
            CREATE TABLE approvals (
                identity TEXT PRIMARY KEY, policy TEXT NOT NULL,
                approval BLOB NOT NULL, authentication_identity TEXT NOT NULL UNIQUE
            );
            CREATE TABLE applied_records (identity TEXT PRIMARY KEY, response BLOB NOT NULL);
            CREATE TRIGGER metadata_no_identity_update BEFORE UPDATE OF instance, ledger_id ON metadata BEGIN
                SELECT RAISE(ABORT, 'immutable owner identity'); END;
            CREATE TRIGGER metadata_no_delete BEFORE DELETE ON metadata BEGIN
                SELECT RAISE(ABORT, 'immutable owner identity'); END;
        """)
        for table in ("artifacts", "approvals", "applied_records"):
            for operation in ("UPDATE", "DELETE"):
                connection.execute(
                    f"CREATE TRIGGER {table}_no_{operation.lower()} BEFORE {operation} "
                    f"ON {table} BEGIN SELECT RAISE(ABORT, 'append-only owner records'); END"
                )
        connection.execute("INSERT INTO metadata VALUES (?, ?, '')", (instance, ledger_id))
        connection.execute("""CREATE TRIGGER metadata_no_insert BEFORE INSERT ON metadata BEGIN
            SELECT RAISE(ABORT, 'single owner identity'); END""")
        connection.commit()
    finally:
        connection.close()


def human_setup_script(
    *,
    proposal_path: Path,
    proposal_sha: str,
    native_path: Path,
    native_sha: str,
    root: str,
    instance: str,
    operator_sid: str,
    role_sha: str,
    functions_raw: bytes | None = None,
) -> bytes:
    """One explicit human setup/enrollment action; never executed by prepare."""

    def literal(value):
        return "'" + str(value).replace("'", "''") + "'"

    values = {
        "PROPOSAL": proposal_path,
        "PROPOSAL_SHA": proposal_sha,
        "NATIVE": native_path,
        "NATIVE_SHA": native_sha,
        "ROOT": root,
        "INSTANCE": instance,
        "SID": operator_sid,
        "ROLE_SHA": role_sha,
    }
    script = r"""# Run only through the exact byte-verified in-memory bootstrap provided to the human.
# Native forms require visible confirmation and actual Windows Hello gestures.
# No unattended consent, policy approval, allocation, qualification or live run.
$ErrorActionPreference = 'Stop'
# Resolve the Windows PowerShell built-ins from this host, not a competing
# PowerShell edition earlier in the inherited module search path.
# Session-global visibility also supplies Get-FileHash's nested Resolve-Path call.
Microsoft.PowerShell.Core\Import-Module -Name ([IO.Path]::Combine($PSHOME, 'Modules\Microsoft.PowerShell.Management\Microsoft.PowerShell.Management.psd1')) -Scope Global -ErrorAction Stop
Microsoft.PowerShell.Core\Import-Module -Name ([IO.Path]::Combine($PSHOME, 'Modules\Microsoft.PowerShell.Utility\Microsoft.PowerShell.Utility.psd1')) -Scope Global -ErrorAction Stop
Set-StrictMode -Version Latest
$proposalPath = @@PROPOSAL@@
$proposalSha = @@PROPOSAL_SHA@@
$nativePath = @@NATIVE@@
$nativeSha = @@NATIVE_SHA@@
$ownerRoot = @@ROOT@@
$ownerInstance = @@INSTANCE@@
$operatorSid = @@SID@@
$roleSha = @@ROLE_SHA@@
$identity = [System.Security.Principal.WindowsIdentity]::GetCurrent()
$principal = [System.Security.Principal.WindowsPrincipal]::new($identity)
if ($identity.User.Value -ne $operatorSid -or -not $principal.IsInRole([System.Security.Principal.WindowsBuiltInRole]::Administrator)) {
    throw 'The exact proposed operator must run this action personally in an elevated PowerShell.'
}
if (Test-Path -LiteralPath $ownerRoot) { throw 'The proposed root already exists. No retry, repair or replacement is performed.' }
if ((Get-FileHash -LiteralPath $proposalPath -Algorithm SHA256).Hash.ToLowerInvariant() -ne $proposalSha) { throw 'Setup proposal changed.' }
Write-Host 'Review the exact principal, dedicated virtual service account, new root and permissions in the native setup form.'
Invoke-PinnedSetupNative -Path $nativePath -Sha256 $nativeSha -Arguments @('setup',$proposalPath,$proposalSha) -InProcessSetup
$registeredNative = Join-Path $ownerRoot 'bin\PolicyOwner.exe'
$rolePath = Join-Path $ownerRoot 'role-proposal.json'
Write-Host 'Review the exact project-role document and complete the Windows Hello prompts. This enrolls the role only.'
Invoke-PinnedSetupNative -Path $registeredNative -Sha256 $nativeSha -Arguments @('enroll',$ownerInstance,$rolePath,$roleSha)
Invoke-PinnedSetupNative -Path $registeredNative -Sha256 $nativeSha -Arguments @('status',$ownerInstance)
Write-Host 'Setup/enrollment action completed. No exact policy was approved and no qualification or live operation was authorized.'
"""
    for key, value in values.items():
        script = script.replace("@@" + key + "@@", literal(value))
    functions = (
        functions_raw
        if functions_raw is not None
        else Path(__file__).with_name("pinned_setup_native.ps1").read_bytes()
    )
    return functions + b"\n" + script.encode("utf-8")


def human_bootstrap_command(action_path: Path, expected_sha: str) -> str:
    """Present these exact commands to the human; do not execute an unpinned file."""
    if re.fullmatch(r"[0-9a-f]{64}", expected_sha) is None:
        raise ValueError("exact action SHA-256 is required")
    path_literal = "'" + str(action_path).replace("'", "''") + "'"
    return (
        "$taSetupBytes = [IO.File]::ReadAllBytes(" + path_literal + ")\n"
        "$taSetupHasher = [Security.Cryptography.SHA256]::Create()\n"
        "try { $taSetupDigest = [BitConverter]::ToString($taSetupHasher.ComputeHash($taSetupBytes)).Replace('-','').ToLowerInvariant() } finally { $taSetupHasher.Dispose() }\n"
        "if ($taSetupDigest -ne '"
        + expected_sha
        + "') { throw 'Reviewed setup action bytes changed.' }\n"
        "& ([ScriptBlock]::Create([Text.UTF8Encoding]::new($false,$true).GetString($taSetupBytes)))\n"
    )


def prepare(args: argparse.Namespace) -> dict:
    from build_policy_owner import canonical, hold_build_inputs, pin, write_new

    from tradingagents.policy_owner_contract import OriginalAcquisitionRegistry, RoleProposal
    from tradingagents.policy_qualification_lifecycle import LineageLedger
    from tradingagents.policy_qualified_contract import (
        BytePin,
        EvidenceRef,
        content_identity,
        strict_json,
    )

    source = Path(__file__).resolve().parents[2]
    output = args.output.resolve(strict=False)
    if output.exists() or output == source or output.is_relative_to(source):
        raise ValueError("setup preparation requires a fresh external output directory")
    if not re.fullmatch(r"(?:test|operational)-[0-9]{8}-[0-9]{3}", args.instance):
        raise ValueError("unknown owner instance")
    role_raw, registry_raw = args.role_proposal.read_bytes(), args.registry.read_bytes()
    role, registry = RoleProposal.read(role_raw), OriginalAcquisitionRegistry.read(registry_raw)
    if role.instance != args.instance or role.operator_sid != args.operator_sid:
        raise ValueError("role proposal differs from the explicitly proposed operator")
    if role.observed_account_name != args.account_name:
        raise ValueError("proposed account name differs from role document")
    build_root = args.native_build.resolve(strict=True)
    build = strict_json((build_root / "build.json").read_bytes())
    if build["contract_version"] != "policy-owner-native-build-v1":
        raise ValueError("unknown native build manifest")
    native_files = {}
    native_names = ["PolicyOwner.exe", "ConditionalRuntimeProtectionHost.exe"]
    if getattr(args, "include_inert_fixture", False):
        if not args.instance.startswith("test-"):
            raise ValueError("the inert supervisor fixture is TEST-only")
        native_names.append("PolicyAuthenticationContracts.exe")
    for name in native_names:
        BytePin.read(build["executables"][name]).check((build_root / name).read_bytes())
        native_files["bin/" + name] = build_root / name
    native_files["native-consumer-source.json"] = build_root / "native-consumer-source.json"
    BytePin.read(
        {key: build["native_consumer_source"][key] for key in ("byte_length", "sha256")}
    ).check(native_files["native-consumer-source.json"].read_bytes())
    # Source pins from the native build must still match. A stale executable is
    # never paired with a newer verifier implementation.
    for item in build["source_files"]:
        if pin(source / item["relative_path"]) != {
            key: item[key] for key in ("byte_length", "sha256")
        }:
            raise ValueError("source differs from the supplied native build")
    service_name = "TA.PolicyOwner." + args.instance
    # `showsid` is read-only and works for a proposed, unregistered service name.
    described = subprocess.run(
        [str(Path(os.environ["SYSTEMROOT"]) / "System32" / "sc.exe"), "showsid", service_name],
        capture_output=True,
        check=True,
        timeout=10,
        creationflags=subprocess.CREATE_NO_WINDOW,
    )
    matches = re.findall(rb"S-1-5-80-(?:[0-9]+-){4}[0-9]+", described.stdout)
    if len(set(matches)) != 1:
        raise ValueError("Windows could not resolve the proposed virtual service SID")
    service_sid = matches[0].decode("ascii")
    owner_root = str(Path(os.environ["PROGRAMDATA"]) / ("TA-PolicyOwner-" + args.instance))
    if Path(owner_root).exists():
        raise ValueError("proposed owner root already exists")
    sources = dict(native_files)
    tracked = (
        subprocess.run(
            [
                "git",
                "-c",
                "safe.directory=" + source.as_posix(),
                "--no-optional-locks",
                "-C",
                str(source),
                "ls-files",
                "-z",
            ],
            capture_output=True,
            check=True,
            timeout=10,
            creationflags=subprocess.CREATE_NO_WINDOW,
        )
        .stdout.decode("utf-8")
        .split("\0")
    )
    relative_sources = {
        name
        for name in tracked
        if name
        and name.startswith(("tradingagents/", "cli/", "scripts/runtime_protection/"))
        and Path(name).suffix in {".py", ".cs", ".json"}
    }
    relative_sources.update(
        str(path.relative_to(source)).replace("\\", "/")
        for path in (source / "scripts/policy_owner").glob("*.*")
        if path.suffix in {".py", ".cs", ".ps1"}
    )
    relative_sources.update(
        str(path.relative_to(source)).replace("\\", "/")
        for path in (source / "tradingagents").glob("*.py")
    )
    for name in sorted(relative_sources):
        sources["source/" + name] = source / name
    python = args.python_root.resolve(strict=True)
    for name in (
        "python.exe",
        "python313.dll",
        "python3.dll",
        "vcruntime140.dll",
        "vcruntime140_1.dll",
    ):
        path = python / name
        if path.is_file():
            sources["python/" + name] = path
    if "python/python.exe" not in sources or "python/python313.dll" not in sources:
        raise ValueError("existing supported CPython 3.13 runtime is incomplete")
    for folder in ("Lib", "DLLs"):
        for path in (python / folder).rglob("*"):
            relative = path.relative_to(python)
            if any(part.lower() in {"site-packages", "__pycache__"} for part in relative.parts):
                continue
            if path.is_file() and path.suffix.lower() not in {".pyc", ".pyo", ".pth"}:
                sources["python/" + relative.as_posix()] = path
    dependencies = args.dependencies.resolve(strict=True)
    packages = (
        "pydantic",
        "pydantic_core",
        "typing_extensions",
        "typing_inspection",
        "annotated_types",
        "packaging",
    )
    for package in packages:
        entries = [dependencies / package, dependencies / (package + ".py")]
        entries.extend(dependencies.glob(package + "-*.dist-info"))
        if not any(entry.exists() for entry in entries):
            raise ValueError("existing owner dependency is absent: " + package)
        for entry in entries:
            for path in [entry] if entry.is_file() else entry.rglob("*"):
                if (
                    path.is_file()
                    and "__pycache__" not in path.parts
                    and path.suffix not in {".pyc", ".pth"}
                ):
                    sources["dependencies/" + path.relative_to(dependencies).as_posix()] = path
    client_dependencies = getattr(args, "client_dependencies", None)
    if client_dependencies is not None:
        if not args.instance.startswith("test-"):
            raise ValueError("the inert client dependency depot is TEST-only")
        client_dependencies = client_dependencies.resolve(strict=True)
        for path in client_dependencies.rglob("*"):
            if path.is_symlink() or getattr(path.lstat(), "st_file_attributes", 0) & 0x400:
                raise ValueError("client dependency depot contains a reparse point")
            if path.is_file():
                if "__pycache__" in path.parts or path.suffix.lower() in {".pyc", ".pyo", ".pth"}:
                    raise ValueError(
                        "client dependency depot contains unreviewed startup/cache input"
                    )
                sources[
                    "test-client-dependencies/" + path.relative_to(client_dependencies).as_posix()
                ] = path
    catalog = strict_json(args.artifact_catalog.read_bytes())
    if (
        set(catalog) != {"contract_version", "artifacts", "execution_authorized"}
        or catalog["contract_version"] != "policy-owner-setup-artifact-catalog-v1"
        or catalog["execution_authorized"] is not False
    ):
        raise ValueError("invalid setup artifact catalog")
    artifact_refs = {}
    for item in catalog["artifacts"]:
        if set(item) != {"reference", "path"}:
            raise ValueError("invalid setup artifact entry")
        ref = EvidenceRef.read(item["reference"])
        if ref.identity in artifact_refs:
            raise ValueError("duplicate setup artifact identity")
        path = Path(item["path"]).resolve(strict=True)
        if pin(path) != ref.pin.model_dump(mode="json"):
            raise ValueError("setup artifact bytes changed")
        artifact_refs[ref.identity] = ref
        sources["artifacts/" + ref.pin.sha256 + ".blob"] = path
    for required in (
        registry.before.artifact,
        registry.after.artifact,
        registry.original_blocker,
        registry.complete_delta_ledger,
    ):
        if artifact_refs.get(required.identity) != required:
            raise ValueError("setup catalog lacks the exact immutable original evidence")
    output.mkdir(exist_ok=False)
    depot = output / "depot"
    depot.mkdir()
    expected_files = {}
    bootstrap_functions = None

    def write_expected(relative: str, raw: bytes) -> None:
        destination = depot / relative
        destination.parent.mkdir(parents=True, exist_ok=True)
        write_new(destination, raw)
        # Bind the intended bytes in memory, never adopt a later rewrite of a
        # mutable staging path as the expected source/runtime/launch pin.
        expected_files[relative] = BytePin.of(raw).model_dump(mode="json")

    # Freeze source/runtime/retained input bytes while copying. These are reads;
    # no original evidence, installed dependency or historical ACL is modified.
    build_source_paths = {source / item["relative_path"] for item in build["source_files"]}
    with hold_build_inputs(
        sorted(set(sources.values()) | build_source_paths | {build_root / "build.json"})
    ):
        if strict_json((build_root / "build.json").read_bytes()) != build:
            raise ValueError("native build manifest changed before setup snapshot")
        for item in build["source_files"]:
            if pin(source / item["relative_path"]) != {
                key: item[key] for key in ("byte_length", "sha256")
            }:
                raise ValueError("build source changed before setup snapshot")
        for name in native_names:
            if pin(build_root / name) != build["executables"][name]:
                raise ValueError("native executable changed before setup snapshot")
        expected_consumer = {
            key: build["native_consumer_source"][key] for key in ("byte_length", "sha256")
        }
        if pin(build_root / "native-consumer-source.json") != expected_consumer:
            raise ValueError("native consumer source binding changed before setup snapshot")
        for relative, path in sorted(sources.items()):
            raw = path.read_bytes()
            write_expected(relative, raw)
            if relative == "source/scripts/policy_owner/pinned_setup_native.ps1":
                bootstrap_functions = raw
    # Native pinned inputs use canonical JSON without a trailing LF. Preserve
    # the sealed semantic identities while deriving these new setup copies.
    write_expected("role-proposal.json", canonical(role.model_dump(mode="json")))
    write_expected("original-acquisitions.json", canonical(registry.model_dump(mode="json")))
    (depot / "state").mkdir()
    ledger_id = content_identity(
        "policy-owner-durable-ledger-registration:v1",
        {"instance": args.instance, "root": owner_root, "service_sid": service_sid},
    )
    LineageLedger.provision(depot / "state/lineage.sqlite3", ledger_id)
    initialize_owner_state(depot / "state/owner.sqlite3", args.instance, ledger_id)
    connection = sqlite3.connect(depot / "state/owner.sqlite3")
    try:
        with connection:
            connection.executemany(
                "INSERT INTO artifacts VALUES (?, ?)",
                [(ref.identity, ref.to_bytes()) for ref in artifact_refs.values()],
            )
    finally:
        connection.close()
    for relative in ("state/owner.sqlite3", "state/lineage.sqlite3"):
        expected_files[relative] = pin(depot / relative)
    worker_files = [
        {"relative_path": relative, **expected_files[relative]}
        for relative in sorted(sources)
        if relative.startswith(("python/", "source/", "dependencies/"))
        or relative == "native-consumer-source.json"
    ]
    write_expected(
        "worker-manifest.json",
        canonical(
            {
                "contract_version": "policy-owner-worker-depot-v1",
                "files": worker_files,
                "execution_authorized": False,
            }
        ),
    )
    observed_files = {
        path.relative_to(depot).as_posix() for path in depot.rglob("*") if path.is_file()
    }
    if observed_files != set(expected_files):
        raise ValueError("unexpected or missing staged setup file; no file is omitted")
    for relative, expected in expected_files.items():
        if pin(depot / relative) != expected:
            raise ValueError("staged setup bytes differ from their frozen source: " + relative)
    # Exercise the complete validation import surface with this exact staged
    # interpreter and dependency set before any setup action can be delivered.
    with hold_build_inputs([depot / relative for relative in sorted(expected_files)]):
        for relative, expected in expected_files.items():
            if pin(depot / relative) != expected:
                raise ValueError("staged setup changed before import validation: " + relative)
        checked = subprocess.run(
            [
                str(depot / "python/python.exe"),
                "-I",
                "-B",
                "-S",
                str(depot / "source/scripts/policy_owner/check_worker_imports.py"),
                str(depot),
            ],
            cwd=depot,
            capture_output=True,
            timeout=45,
            creationflags=subprocess.CREATE_NO_WINDOW,
            env={
                "SystemRoot": os.environ["SYSTEMROOT"],
                "PATH": str(depot / "python")
                + ";"
                + str(Path(os.environ["SYSTEMROOT"]) / "System32"),
                "TEMP": str(output),
                "TMP": str(output),
            },
        )
    write_new(output / "worker-import-check.json", checked.stdout)
    write_new(output / "worker-import-check.stderr", checked.stderr)
    if checked.returncode or strict_json(checked.stdout).get("passed") is not True:
        raise ValueError("staged worker import closure failed; no setup action emitted")
    proposal = {
        "contract_version": "policy-owner-setup-proposal-v1",
        "instance": args.instance,
        "root": owner_root,
        "operator_sid": args.operator_sid,
        "observed_account_name": args.account_name,
        "service_sid": service_sid,
        "role_proposal": role.model_dump(mode="json"),
        "ledger_identity": ledger_id,
        "files": [
            {"relative_path": relative, **expected_files[relative]}
            for relative in sorted(expected_files)
        ],
        "mechanism": "WINDOWS_HELLO_WEBAUTHN_AND_DEDICATED_VIRTUAL_SERVICE_ACCOUNT",
        "registration_is_approval": False,
        "execution_authorized": False,
    }
    proposal["identity"] = content_identity("policy-owner-setup-proposal:v1", proposal)
    proposal_raw = canonical(proposal)
    proposal_pin = BytePin.of(proposal_raw).model_dump(mode="json")
    write_new(output / "SETUP-PROPOSAL.json", proposal_raw)
    write_new(output / "windows-proposed-service-sid.txt", described.stdout)
    script = output / "RUN-HUMAN-SETUP.ps1"
    if bootstrap_functions is None:
        raise ValueError("held native bootstrap source is missing")
    script_raw = human_setup_script(
        proposal_path=output / "SETUP-PROPOSAL.json",
        proposal_sha=proposal_pin["sha256"],
        native_path=depot / "bin/PolicyOwner.exe",
        native_sha=build["executables"]["PolicyOwner.exe"]["sha256"],
        root=owner_root,
        instance=args.instance,
        operator_sid=args.operator_sid,
        role_sha=expected_files["role-proposal.json"]["sha256"],
        functions_raw=bootstrap_functions,
    )
    write_new(script, script_raw)
    script_pin = BytePin.of(script_raw).model_dump(mode="json")
    bootstrap = human_bootstrap_command(script, script_pin["sha256"])
    write_new(output / "HUMAN-BOOTSTRAP.txt", bootstrap.encode("utf-8"))
    return {
        "setup_proposal": str(output / "SETUP-PROPOSAL.json"),
        **proposal_pin,
        "native_setup": str(depot / "bin/PolicyOwner.exe"),
        "human_setup_action": str(script),
        "human_setup_action_pin": script_pin,
        "human_setup_bootstrap_commands": bootstrap,
        "principal_enrolled": False,
        "service_installed": False,
        "execution_authorized": False,
    }


def main() -> int:
    if os.name != "nt" or not (
        sys.flags.isolated and sys.flags.no_site and sys.dont_write_bytecode
    ):
        raise ValueError("setup preparation requires existing Windows CPython with -I -B -S")
    parser = argparse.ArgumentParser(description=__doc__)
    for name in (
        "output",
        "native-build",
        "python-root",
        "dependencies",
        "registry",
        "role-proposal",
        "artifact-catalog",
    ):
        parser.add_argument("--" + name, type=Path, required=True)
    for name in ("instance", "operator-sid", "account-name"):
        parser.add_argument("--" + name, required=True)
    parser.add_argument("--client-dependencies", type=Path)
    parser.add_argument("--include-inert-fixture", action="store_true")
    args = parser.parse_args()
    sys.path[:0] = [
        str(Path(__file__).resolve().parents[2]),
        str(Path(__file__).resolve().parent),
        str(args.dependencies.resolve(strict=True)),
    ]
    print(json.dumps(prepare(args), sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

"""Build the native owner/consumer from pinned local source with existing CSC.

Build output is fresh and external. This does not install a service, change a
permission, enroll anyone, create a policy decision, or start an application.
"""

from __future__ import annotations

import argparse
import ctypes
import json
import os
import re
import subprocess
import sys
from contextlib import contextmanager
from hashlib import sha256
from pathlib import Path

CONSUMER_SOURCES = (
    "scripts/runtime_protection/RuntimeProtectionHost.cs",
    "scripts/policy_owner/OwnerTransport.cs",
    "scripts/policy_owner/ConditionalRuntimeProtectionHost.cs",
    "tradingagents/runtime_protection.py",
    "tradingagents/runtime_protection_handoff.py",
    "tradingagents/policy_qualified_native.py",
    "tradingagents/policy_coverage_contract.py",
    "tradingagents/policy_coverage_models.py",
    "tradingagents/policy_coverage_verification.py",
    "tradingagents/policy_coverage_native.py",
    "tradingagents/policy_coverage_schema.py",
    "tradingagents/policy_evidence_acquisition.py",
    "tradingagents/policy_evidence_contract.py",
    "tradingagents/policy_qualified_results.py",
    "tradingagents/windows_policy_owner.py",
    "scripts/policy_owner/owner_worker.py",
    "scripts/policy_owner/build_policy_owner.py",
)
NATIVE_SOURCES = (
    "scripts/runtime_protection/RuntimeProtectionHost.cs",
    "scripts/policy_owner/OwnerTransport.cs",
    "scripts/policy_owner/ConditionalRuntimeProtectionHost.cs",
    "scripts/policy_owner/WindowsPolicyAuthentication.cs",
    "scripts/policy_owner/PolicyOwnerMain.cs",
    "scripts/policy_owner/AdmissionV2.cs",
    "scripts/policy_owner/AdmissionV2Platform.cs",
)
ADMISSION_SOURCES = (
    "tradingagents/policy_owner_admission_v2.py",
    "tradingagents/policy_owner_admission_crypto.py",
    "tradingagents/policy_owner_admission_windows.py",
    "tradingagents/policy_owner_admission_migration.py",
    "scripts/policy_owner/migrate_admission_v2.py",
    "scripts/policy_owner/build_admission_depot.py",
    "scripts/policy_owner/check_worker_imports.py",
)
CONSUMER_SOURCES = tuple(sorted(set(CONSUMER_SOURCES) | set(NATIVE_SOURCES) | set(ADMISSION_SOURCES)))


def canonical(value: object) -> bytes:
    return json.dumps(
        value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False
    ).encode("utf-8")


def pin(path: Path) -> dict:
    raw = path.read_bytes()
    return {"byte_length": len(raw), "sha256": sha256(raw).hexdigest()}


@contextmanager
def hold_build_inputs(paths: list[Path], *, system_compiler: Path | None = None):
    """Deny data writes/replacement of exact compiler inputs through compilation."""
    from ctypes import wintypes

    class Information(ctypes.Structure):
        _fields_ = [
            (name, wintypes.DWORD)
            for name in (
                "attributes",
                "creation_low",
                "creation_high",
                "access_low",
                "access_high",
                "write_low",
                "write_high",
                "volume",
                "size_high",
                "size_low",
                "links",
                "index_high",
                "index_low",
            )
        ]

    api = ctypes.WinDLL("kernel32", use_last_error=True)
    api.CreateFileW.argtypes = [
        wintypes.LPCWSTR,
        wintypes.DWORD,
        wintypes.DWORD,
        ctypes.c_void_p,
        wintypes.DWORD,
        wintypes.DWORD,
        wintypes.HANDLE,
    ]
    api.CreateFileW.restype = wintypes.HANDLE
    api.GetFileInformationByHandle.argtypes = [wintypes.HANDLE, ctypes.POINTER(Information)]
    api.GetFileInformationByHandle.restype = wintypes.BOOL
    api.CloseHandle.argtypes = [wintypes.HANDLE]
    held = []
    directories = sorted(
        {parent for path in paths for parent in path.parents},
        key=lambda path: (len(path.parts), str(path)),
    )
    try:
        for path, directory in [
            *((path, True) for path in directories),
            *((path, False) for path in paths),
        ]:
            handle = api.CreateFileW(
                str(path),
                0x80 if directory else 0x80000000,
                3 if directory else 1,
                None,
                3,
                0x02200000,
                None,
            )
            if handle == ctypes.c_void_p(-1).value:
                raise ctypes.WinError(ctypes.get_last_error())
            held.append(handle)
            value = Information()
            if not api.GetFileInformationByHandle(handle, ctypes.byref(value)):
                raise ctypes.WinError(ctypes.get_last_error())
            if (
                value.attributes & 0x400
                or bool(value.attributes & 0x10) != directory
                or (not directory and value.links != 1 and path != system_compiler)
            ):
                raise ValueError(
                    "build input has a reparse, type or hard-link mismatch: " + str(path)
                )
        yield
    finally:
        for handle in reversed(held):
            api.CloseHandle(handle)


def write_new(path: Path, raw: bytes) -> None:
    with path.open("xb") as stream:
        stream.write(raw)
        stream.flush()
        os.fsync(stream.fileno())


def build(output: Path, *, fixture: bool = False, source_commit: str | None = None) -> dict:
    if os.name != "nt" or not (
        sys.flags.isolated and sys.flags.no_site and sys.dont_write_bytecode
    ):
        raise ValueError("build requires an existing Windows interpreter with -I -B -S")
    source = Path(__file__).resolve().parents[2]
    if source_commit is not None:
        if re.fullmatch("[0-9a-f]{40}", source_commit) is None or source_commit == "0" * 40:
            raise ValueError("exact committed source identity required")
        for arguments, expected in ((["rev-parse", "HEAD"], source_commit), (["status", "--porcelain"], "")):
            result = subprocess.run(["git", "-C", str(source), *arguments], capture_output=True, text=True, check=True, timeout=10,
                                    creationflags=subprocess.CREATE_NO_WINDOW)
            if result.stdout.strip() != expected:
                raise ValueError("source commit/worktree differs from the declared final build")
    output = output.resolve(strict=False)
    if output == source or output.is_relative_to(source) or output.exists():
        raise ValueError(
            "native build output must be a fresh directory outside the source worktree"
        )
    output.mkdir(parents=False, exist_ok=False)
    compiler = Path(os.environ["SYSTEMROOT"]) / "Microsoft.NET/Framework64/v4.0.30319/csc.exe"
    if not compiler.is_file():
        raise ValueError("existing Windows .NET Framework x64 compiler is unavailable")
    names = set(NATIVE_SOURCES) | set(CONSUMER_SOURCES) | {"cli/main.py"}
    if fixture:
        names.add("tests/fixtures/policy_owner_admission_v2/AdmissionV2Contracts.cs")
    snapshot = output / "inputs"
    with hold_build_inputs([source / name for name in sorted(names)]):
        for name in sorted(names):
            destination = snapshot / name
            destination.parent.mkdir(parents=True, exist_ok=True)
            write_new(destination, (source / name).read_bytes())
    source_pins = {name: pin(snapshot / name) for name in sorted(names)}
    consumer = {
        "contract_version": "policy-owner-admission-consumer-source-v2",
        "files": [
            {"relative_path": name, **source_pins[name]} for name in sorted(CONSUMER_SOURCES)
        ],
    }
    consumer["identity"] = (
        "policy-owner-admission-consumer-source:v2:sha256:"
        + sha256(canonical(consumer)).hexdigest()
    )
    write_new(output / "native-consumer-source.json", canonical(consumer))
    stamps = {
        "RuntimeProtectionSourceSha256": source_pins[NATIVE_SOURCES[0]]["sha256"],
        "RuntimeProtectionCliSha256": source_pins["cli/main.py"]["sha256"],
        "RuntimeProtectionContractSha256": source_pins["tradingagents/runtime_protection.py"][
            "sha256"
        ],
        "RuntimeProtectionHandoffSha256": source_pins[
            "tradingagents/runtime_protection_handoff.py"
        ]["sha256"],
        "PolicyQualifiedNativeConsumerSourceIdentity": consumer["identity"],
        "PolicyOwnerAdmissionSourceCommit": source_commit or "DEVELOPMENT_UNCOMMITTED_NOT_DEPLOYABLE",
        "PolicyOwnerAdmissionMode": "ENROLLMENT_ONLY",
    }
    stamp_path = output / "BuildIdentity.cs"
    write_new(
        stamp_path,
        "\n".join(
            '[assembly: System.Reflection.AssemblyMetadataAttribute("'
            + key
            + '", "'
            + value
            + '")]'
            for key, value in sorted(stamps.items())
        ).encode("ascii"),
    )
    common = [
        str(compiler),
        "/nologo",
        "/target:exe",
        "/platform:x64",
        "/r:System.Web.Extensions.dll",
        "/r:System.ServiceProcess.dll",
        "/r:System.Windows.Forms.dll",
        "/r:System.Drawing.dll",
    ]
    sources = [str(snapshot / name) for name in NATIVE_SOURCES] + [str(stamp_path)]
    targets = {
        "PolicyOwner.exe": "TradingAgents.PolicyOwner.PolicyOwnerMain",
        "ConditionalRuntimeProtectionHost.exe": "TradingAgents.PolicyOwner.ConditionalRuntimeProtectionHost",
    }
    if fixture:
        targets["AdmissionV2Contracts.TEST.exe"] = "AdmissionV2Contracts"
    results = {}
    for name, entrypoint in targets.items():
        extra = (
            [str(snapshot / "tests/fixtures/policy_owner_admission_v2/AdmissionV2Contracts.cs")]
            if entrypoint == "AdmissionV2Contracts"
            else []
        )
        defines = "OWNER_ADMISSION_V2;OWNER_ADMISSION_V2_TEST" if entrypoint == "AdmissionV2Contracts" else "OWNER_ADMISSION_V2"
        command = [*common, "/define:" + defines, "/main:" + entrypoint, "/out:" + str(output / name), *sources, *extra]
        # Windows services its existing compiler through a WinSxS hard link.
        # The open file still denies writes/replacement through either name.
        with hold_build_inputs(
            [Path(path) for path in [*sources, *extra]] + [compiler], system_compiler=compiler
        ):
            for relative, expected in source_pins.items():
                if pin(snapshot / relative) != expected:
                    raise ValueError("immutable compiler snapshot drifted before compilation")
            completed = subprocess.run(
                command,
                cwd=output,
                capture_output=True,
                check=False,
                timeout=45,
                creationflags=subprocess.CREATE_NO_WINDOW,
            )
        write_new(output / (name + ".build.stdout"), completed.stdout)
        write_new(output / (name + ".build.stderr"), completed.stderr)
        if completed.returncode != 0:
            raise RuntimeError(
                "native build failed; retained compiler output: "
                + str(output / (name + ".build.stdout"))
            )
        results[name] = pin(output / name)
    manifest = {
        "contract_version": "policy-owner-admission-native-build-v2",
        "source_commit": source_commit,
        "development_build_not_deployable": source_commit is None,
        "mode": "ENROLLMENT_ONLY",
        "source_root": str(source),
        "source_files": [{"relative_path": name, **value} for name, value in source_pins.items()],
        "compiler": {"path": str(compiler), **pin(compiler)},
        "executables": results,
        "native_consumer_source": {
            "identity": consumer["identity"],
            **pin(output / "native-consumer-source.json"),
        },
        "fixture_entrypoint_included": fixture,
        "service_installed": False,
        "principal_enrolled": False,
        "qualification_performed": False,
        "execution_authorized": False,
    }
    write_new(output / "build.json", canonical(manifest))
    return manifest


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--include-inert-fixture", action="store_true")
    parser.add_argument("--source-commit")
    args = parser.parse_args()
    result = build(args.output, fixture=args.include_inert_fixture, source_commit=args.source_commit)
    print(
        json.dumps(
            {
                "output": str(args.output),
                "executables": result["executables"],
                "execution_authorized": False,
            },
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

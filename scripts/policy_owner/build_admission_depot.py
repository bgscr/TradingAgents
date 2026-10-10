"""Create an external candidate depot from committed v2 code and a sealed runtime.

This is a build, not setup or migration. It never opens installed owner state or
executes the produced service, worker, maintenance adapter, or authenticator.
"""
from __future__ import annotations

import argparse
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import sys

SOURCES = (
    "scripts/policy_owner/owner_worker.py",
    "scripts/policy_owner/migrate_admission_v2.py",
    "tradingagents/policy_owner_admission_v2.py",
    "tradingagents/policy_owner_admission_crypto.py",
    "tradingagents/policy_owner_admission_windows.py",
    "tradingagents/policy_owner_admission_migration.py",
)
PREDECESSOR_MANIFEST_PIN = {"byte_length": 712286, "sha256": "c1048a67ba4c49a45a7848fbce5b93ea44fcd0df27fd19bb75aac672a66bfcc5"}


def main() -> int:
    if os.name != "nt" or not (sys.flags.isolated and sys.flags.no_site and sys.dont_write_bytecode):
        raise ValueError("existing Windows Python -I -B -S required")
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--native-build", type=Path, required=True)
    parser.add_argument("--predecessor-packet", type=Path, required=True)
    parser.add_argument("--source-commit", required=True)
    args = parser.parse_args()
    source = Path(__file__).resolve().parents[2]
    spec = importlib.util.spec_from_file_location("admission_build_helpers", Path(__file__).with_name("build_policy_owner.py"))
    helper = importlib.util.module_from_spec(spec); spec.loader.exec_module(helper)
    for arguments, expected in ((["rev-parse", "HEAD"], args.source_commit), (["status", "--porcelain"], "")):
        result = subprocess.run(["git", "-C", str(source), *arguments], capture_output=True, text=True, check=True, timeout=10, creationflags=subprocess.CREATE_NO_WINDOW)
        if result.stdout.strip() != expected:
            raise ValueError("source commit/clean worktree mismatch")
    output = args.output.absolute()
    if not output.is_absolute() or output.exists() or output == source or output.is_relative_to(source):
        raise ValueError("fresh external build output required")
    packet = args.predecessor_packet.absolute()
    manifest_path = packet / "DELIVERY-MANIFEST.json"
    with helper.hold_build_inputs([manifest_path]):
        if helper.pin(manifest_path) != PREDECESSOR_MANIFEST_PIN:
            raise ValueError("historical sealed packet manifest pin mismatch")
        old = json.loads(manifest_path.read_bytes())
    expected = {item["relative_path"]: {"byte_length": item["byte_length"], "sha256": item["sha256"]} for item in old["files"]}
    actual, actual_directories = set(), set()
    for base, directories, files in os.walk(packet, topdown=True, followlinks=False):
        for name in [*directories, *files]:
            path = Path(base) / name
            info = path.lstat()
            if getattr(info, "st_file_attributes", 0) & 0x400:
                raise ValueError("sealed predecessor reparse entry: " + str(path))
            relative = path.relative_to(packet).as_posix()
            if name in directories: actual_directories.add(relative)
            else: actual.add(relative)
    if actual - set(old["inventory_exclusions"]) != set(expected):
        raise ValueError("sealed predecessor full file inventory mismatch")
    old_directories = set(item if isinstance(item, str) else item["relative_path"] for item in old["directories"])
    if actual_directories != old_directories:
        raise ValueError("sealed predecessor directory inventory mismatch")
    # Check every retained member, not only the files selected for this build.
    with helper.hold_build_inputs([packet / relative for relative in expected] + [manifest_path]):
        for relative, expected_pin in expected.items():
            if helper.pin(packet / relative) != expected_pin:
                raise ValueError("sealed predecessor changed: " + relative)
    native = json.loads((args.native_build / "build.json").read_bytes())
    if native["source_commit"] != args.source_commit or native["development_build_not_deployable"]:
        raise ValueError("final committed native build required")
    native_source_pins = {item["relative_path"]: {"byte_length": item["byte_length"], "sha256": item["sha256"]} for item in native["source_files"]}
    if not set(SOURCES) <= set(native_source_pins):
        raise ValueError("native build does not bind the complete new worker source closure")
    selected = {relative: expected_pin for relative, expected_pin in expected.items() if relative.startswith("setup-bundle/depot/python/")}
    if "setup-bundle/depot/python/python.exe" not in selected:
        raise ValueError("complete existing sealed Python runtime missing")
    references = {"baseline-owner.sqlite3": "state/owner.sqlite3", "baseline-lineage.sqlite3": "state/lineage.sqlite3",
                  "original-role-proposal.json": "role-proposal.json"}
    inputs = [source / name for name in SOURCES] + [packet / name for name in selected]
    inputs += [packet / ("setup-bundle/depot/" + value) for value in references.values()]
    inputs += [args.native_build / name for name in ("PolicyOwner.exe", "ConditionalRuntimeProtectionHost.exe")]
    inputs += [args.native_build / "build.json", manifest_path]
    output.mkdir()
    depot = output / "depot"; depot.mkdir()
    origins = []
    with helper.hold_build_inputs(inputs):
        for relative, expected_pin in selected.items():
            original = packet / relative
            if helper.pin(original) != expected_pin:
                raise ValueError("held sealed runtime changed")
            destination = depot / relative.removeprefix("setup-bundle/depot/")
            destination.parent.mkdir(parents=True, exist_ok=True)
            helper.write_new(destination, original.read_bytes())
            origins.append({"source": str(original), "relative_path": destination.relative_to(depot).as_posix(), **expected_pin})
        for name in SOURCES:
            if helper.pin(source / name) != native_source_pins[name]:
                raise ValueError("worker source differs from the same pinned native build: " + name)
            destination = depot / "source" / name; destination.parent.mkdir(parents=True, exist_ok=True)
            helper.write_new(destination, (source / name).read_bytes())
        for name in ("PolicyOwner.exe", "ConditionalRuntimeProtectionHost.exe"):
            original = args.native_build / name
            if helper.pin(original) != native["executables"][name]:
                raise ValueError("native artifact changed after its build")
            destination = depot / "bin" / name; destination.parent.mkdir(parents=True, exist_ok=True)
            helper.write_new(destination, original.read_bytes())
        for name, relative in references.items():
            original = packet / "setup-bundle" / "depot" / relative
            if helper.pin(original) != expected["setup-bundle/depot/" + relative]:
                raise ValueError("fixed baseline reference pin mismatch")
            destination = depot / "reference" / name; destination.parent.mkdir(parents=True, exist_ok=True)
            helper.write_new(destination, original.read_bytes())
    files = []
    for subtree in ("python", "source", "bin"):
        for path in sorted((depot / subtree).rglob("*")):
            if path.is_file():
                files.append({"relative_path": path.relative_to(depot).as_posix(), **helper.pin(path)})
    worker_manifest = {"contract_version": "policy-owner-admission-worker-depot-v2", "source_commit": args.source_commit,
                       "files": sorted(files, key=lambda item: item["relative_path"]), "execution_authorized": False}
    helper.write_new(depot / "worker-manifest.json", helper.canonical(worker_manifest))
    helper.write_new(output / "runtime-origins.json", helper.canonical({"predecessor_manifest_pin": PREDECESSOR_MANIFEST_PIN,
        "verified_predecessor_members": len(expected), "verified_predecessor_directories": len(actual_directories), "files": origins}))
    result = {"contract_version": "policy-owner-admission-depot-build-v2", "source_commit": args.source_commit,
              "worker_manifest_pin": helper.pin(depot / "worker-manifest.json"), "native_pin": native["executables"]["PolicyOwner.exe"],
              "baseline_pins": {"owner": helper.pin(depot / "reference" / "baseline-owner.sqlite3"), "ledger": helper.pin(depot / "reference" / "baseline-lineage.sqlite3")},
              "runtime_origins_pin": helper.pin(output / "runtime-origins.json"), "installed": False,
              "platform_validated": False, "execution_authorized": False}
    helper.write_new(output / "depot-build.json", helper.canonical(result))
    print(json.dumps(result, sort_keys=True)); return 0


if __name__ == "__main__":
    raise SystemExit(main())

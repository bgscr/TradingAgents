"""Execute real verifier algorithms with delivered bytes and synthetic fixtures.

No registered owner, Windows Hello, service setup or qualification runner is used.
The dependency probe mode only diagnoses import isolation; it grants no admission.
"""

from __future__ import annotations

import argparse
import hashlib
import importlib
import json
import os
import sys
from pathlib import Path


def module_inventory(depot: Path) -> dict:
    backed, builtins, frozen, namespace = [], [], [], []
    for name, module in sorted(sys.modules.items()):
        if module is None:
            continue
        spec = getattr(module, "__spec__", None)
        origin = getattr(spec, "origin", None)
        if origin == "built-in":
            builtins.append(name)
        elif origin == "frozen":
            frozen.append(name)
        elif getattr(module, "__file__", None):
            path = Path(module.__file__).resolve(strict=True)
            if not path.is_relative_to(depot):
                raise ValueError(
                    "file-backed module escaped delivered payload: " + name + "=" + str(path)
                )
            raw = path.read_bytes()
            backed.append(
                {
                    "module": name,
                    "path": str(path),
                    "relative_path": path.relative_to(depot).as_posix(),
                    "sha256": hashlib.sha256(raw).hexdigest(),
                    "byte_length": len(raw),
                }
            )
        else:
            namespace.append(name)
    return {
        "file_backed": backed,
        "built_in": builtins,
        "frozen": frozen,
        "namespace_or_virtual": namespace,
    }


def run(depot: Path, scratch: Path, manifest_sha: str, *, dependency_probe: bool = False) -> dict:
    depot, scratch = depot.resolve(strict=True), scratch.resolve(strict=True)
    if Path(sys.executable).resolve(strict=True) != depot / "python/python.exe":
        raise ValueError("the actual delivered interpreter is required")
    sys.path[:0] = [str(depot / "source"), str(depot / "dependencies")]
    if any(not Path(entry).resolve(strict=False).is_relative_to(depot) for entry in sys.path):
        raise ValueError("module search escaped the delivered payload")
    os.environ["PYTHON_DOTENV_DISABLED"] = "1"

    def writable(value: object) -> None:
        if isinstance(value, int):
            return
        path = Path(os.fsdecode(value)).resolve(strict=False)
        if not path.is_relative_to(scratch):
            raise RuntimeError("predeployment fixture write escaped scratch")

    def audit(event: str, values: tuple) -> None:
        if event in {
            "subprocess.Popen",
            "os.system",
            "os.startfile",
            "os.startfile/2",
            "socket.connect",
            "socket.bind",
            "socket.getaddrinfo",
            "socket.gethostbyname",
        }:
            raise RuntimeError("predeployment verifier forbids native/provider/network execution")
        if (
            event == "open"
            and values[2] & (os.O_WRONLY | os.O_RDWR | os.O_CREAT | os.O_TRUNC | os.O_APPEND)
            or event in {"os.mkdir", "os.remove", "os.rmdir", "os.chmod", "os.utime", "os.truncate"}
        ):
            writable(values[0])
        elif event in {"os.rename", "os.link", "os.symlink"}:
            writable(values[0])
            writable(values[1])
        elif event == "sqlite3.connect":
            from urllib.parse import unquote, urlparse

            target = str(values[0])
            if target.startswith("file:"):
                target = unquote(urlparse(target).path).lstrip("/")
            writable(target)

    sys.addaudithook(audit)
    report = {
        "contract_version": "policy-owner-predeployment-verifier-check-v1",
        "interpreter": sys.executable,
        "python_version": sys.version,
        "startup_flags": {
            "isolated": sys.flags.isolated,
            "no_site": sys.flags.no_site,
            "dont_write_bytecode": sys.dont_write_bytecode,
        },
        "module_search": list(sys.path),
        "synthetic_fixture_authority": True,
        "service_identity_exercised": False,
        "windows_hello_exercised": False,
        "real_approval_or_claim_created": False,
        "qualification_runner_executed": False,
        "execution_authorized": False,
    }
    if dependency_probe:
        try:
            packaging = importlib.import_module("packaging")
            report.update(
                imported=True, origin=packaging.__file__, packaging_version=packaging.__version__
            )
            module_inventory(depot)
        except ModuleNotFoundError as error:
            report.update(imported=False, rejection=str(error))
        report["diagnostic_only_no_verifier_admission"] = True
        return report
    manifest_raw = (depot / "worker-manifest.json").read_bytes()
    if hashlib.sha256(manifest_raw).hexdigest() != manifest_sha:
        raise ValueError("worker manifest byte pin mismatch")
    manifest = json.loads(manifest_raw)
    for entry in manifest["files"]:
        path = depot / entry["relative_path"]
        if not path.resolve(strict=False).is_relative_to(depot):
            raise ValueError("manifest path escape")
        raw = path.read_bytes()
        if len(raw) != entry["byte_length"] or hashlib.sha256(raw).hexdigest() != entry["sha256"]:
            raise ValueError("delivered dependency/source byte mismatch: " + entry["relative_path"])
    import packaging
    from scripts.policy_owner.predeployment_fixtures import first_stage
    from tradingagents.offline_runtime_bundle import (
        OfflineBundleContractError,
        verify_offline_runtime_bundle,
    )
    from tradingagents.runtime_protection import validate_conditional_attempt

    positive = first_stage(scratch / "SYNTHETIC-positive")
    tamper = first_stage(scratch / "SYNTHETIC-tamper")
    wheel = next(
        entry.relative_path
        for entry in tamper["manifest"].files
        if entry.relative_path.endswith(".whl")
    )
    wheel_path = tamper["case"].owner.bundle_root / wheel
    with wheel_path.open("ab") as stream:
        stream.write(b"SYNTHETIC CORRUPTION ONLY")
    rejected = False
    try:
        verify_offline_runtime_bundle(
            tamper["case"].owner.bundle_root, expected_source=tamper["source"]
        )
    except OfflineBundleContractError as error:
        rejected = True
        report["changed_fixture_bundle_rejection"] = str(error)
    if not rejected:
        raise AssertionError("real runtime verifier accepted changed fixture bytes")
    malformed_rejected = False
    try:
        validate_conditional_attempt(
            {"contract_version": "runtime-protection-attempt-v1", "execution_authorized": False}
        )
    except ValueError:
        malformed_rejected = True
    if not malformed_rejected:
        raise AssertionError("conditional consumer accepted stripped legacy input")
    report.update(
        passed=True,
        packaging_version=packaging.__version__,
        packaging_origin=packaging.__file__,
        actual_policy_verifier_claim_begin_complete_executed=True,
        synthetic_completion_event=positive["completion_event"],
        actual_source_and_runtime_verification_passed=True,
        actual_runtime_bundle_consumer_passed=True,
        changed_fixture_bundle_rejected=True,
        stripped_conditional_input_rejected=True,
        pending=[
            "actual service identity and new enrollment",
            "fresh authenticated exact test decisions",
            "complete terminal conditional result and native inert handoff through registered owner",
        ],
    )
    report["modules"] = module_inventory(depot)
    expected = {entry["relative_path"]: entry for entry in manifest["files"]}
    for module in report["modules"]["file_backed"]:
        entry = expected.get(module["relative_path"])
        if (
            entry is None
            or entry["sha256"] != module["sha256"]
            or entry["byte_length"] != module["byte_length"]
        ):
            raise ValueError(
                "loaded file-backed module was not exactly delivered: " + module["module"]
            )
    return report


def main() -> int:
    if not (sys.flags.isolated and sys.flags.no_site and sys.dont_write_bytecode):
        raise ValueError("use the delivered interpreter with -I -B -S")
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--depot", type=Path, required=True)
    parser.add_argument("--scratch", type=Path, required=True)
    parser.add_argument("--manifest-sha256", required=True)
    parser.add_argument("--dependency-probe", action="store_true")
    args = parser.parse_args()
    try:
        report = run(
            args.depot, args.scratch, args.manifest_sha256, dependency_probe=args.dependency_probe
        )
        print(json.dumps(report, sort_keys=True))
        return 0
    except Exception as error:
        print(
            json.dumps(
                {
                    "passed": False,
                    "error_type": type(error).__name__,
                    "error": str(error),
                    "execution_authorized": False,
                },
                sort_keys=True,
            )
        )
        return 2


if __name__ == "__main__":
    raise SystemExit(main())

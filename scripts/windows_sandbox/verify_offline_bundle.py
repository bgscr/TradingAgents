"""Guest-side stdlib verification for an already byte-verified ISO-02 bundle."""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import stat
import sys
import zipfile
from pathlib import Path, PurePosixPath

if sys.version_info >= (3, 11):
    import tomllib
else:
    # Host-side compatibility checks also run on 3.10. The qualified 3.13
    # guest continues to use only the standard library.
    import tomli as tomllib

_NATIVE_SUFFIXES = frozenset({".dll", ".exe", ".pyd"})
_PE_MACHINE_NAMES = {0x014C: "I386", 0x8664: "AMD64", 0xAA64: "ARM64"}
_REVIEWED_CROSS_ARCH_NATIVE_EXCEPTIONS = frozenset(
    {
        ("pip-25.2-py3-none-any.whl", "pip/_vendor/distlib/t32.exe", "6b4195e640a85ac32eb6f9628822a622057df1e459df7c17a12f97aeabc9415b", "I386"),
        ("pip-25.2-py3-none-any.whl", "pip/_vendor/distlib/t64-arm.exe", "ebc4c06b7d95e74e315419ee7e88e1d0f71e9e9477538c00a93a9ff8c66a6cfc", "ARM64"),
        ("pip-25.2-py3-none-any.whl", "pip/_vendor/distlib/w32.exe", "47872cc77f8e18cf642f868f23340a468e537e64521d9a3a416c8b84384d064b", "I386"),
        ("pip-25.2-py3-none-any.whl", "pip/_vendor/distlib/w64-arm.exe", "c5dc9884a8f458371550e09bd396e5418bf375820a31b9899f6499bf391c7b2e", "ARM64"),
        ("setuptools-84.0.0-py3-none-any.whl", "setuptools/cli-32.exe", "32acc1bc543116cbe2cff10cb867772df2f254ff2634c870aef0b46c4b696fdb", "I386"),
        ("setuptools-84.0.0-py3-none-any.whl", "setuptools/cli-arm64.exe", "b9a7d08da880dfac8bcf548eba4b06fb59b6f09b17d33148a0f6618328926c61", "ARM64"),
        ("setuptools-84.0.0-py3-none-any.whl", "setuptools/cli.exe", "32acc1bc543116cbe2cff10cb867772df2f254ff2634c870aef0b46c4b696fdb", "I386"),
        ("setuptools-84.0.0-py3-none-any.whl", "setuptools/gui-32.exe", "85dae1e95d77845f2cb59bcac3d4afe74bbe4c91a9bcc5bf4a71cd43104dbe7c", "I386"),
        ("setuptools-84.0.0-py3-none-any.whl", "setuptools/gui-arm64.exe", "e694f4743405c8b5926ff457db6fe7f1a12dec7c16a9c3864784d3f4e07ae097", "ARM64"),
        ("setuptools-84.0.0-py3-none-any.whl", "setuptools/gui.exe", "85dae1e95d77845f2cb59bcac3d4afe74bbe4c91a9bcc5bf4a71cd43104dbe7c", "I386"),
    }
)
_EXPECTED_MANIFEST_KEYS = {
    "abi_tag",
    "bootstrap_relative_path",
    "bootstrap_support_identity",
    "bundle_identity",
    "contract_version",
    "dependency_lock_identity",
    "dependency_lock_relative_path",
    "dependency_roots",
    "files",
    "install_policy",
    "iso01_runtime_bundle_input_contract_version",
    "platform_tag",
    "portable_git_identity",
    "portable_git_archive_relative_path",
    "portable_git_relative_path",
    "portable_git_required",
    "project_metadata_sha256",
    "project_metadata_relative_path",
    "python_implementation",
    "python_installer_relative_path",
    "python_runtime_identity",
    "python_tag",
    "python_version",
    "reviewed_input_identity",
    "reviewed_source_manifest_relative_path",
    "runtime_artifact_lock_relative_path",
    "source",
    "supported_windows_architecture",
    "verifier_relative_path",
    "wheelhouse_inventory",
    "wheelhouse_inventory_identity",
}
_BOOTSTRAP_TOOL_ROOTS = ("pip==25.2", "wheel==0.46.3")
_TRACKED_BUNDLE_BINDINGS = {
    "provenance/iso02-artifact-lock.json": (
        "scripts/windows_sandbox/iso02-artifact-lock.json"
    ),
    "provenance/pyproject.toml": "pyproject.toml",
    "bootstrap/install-runtime.ps1": (
        "scripts/windows_sandbox/offline_bundle_bootstrap.ps1"
    ),
    "bootstrap/verify_bundle.py": "scripts/windows_sandbox/verify_offline_bundle.py",
    "locks/requirements-win-cp313.txt": (
        "scripts/windows_sandbox/iso02-requirements-win-cp313.txt"
    ),
}


def _canonical_json_bytes(value: object) -> bytes:
    return json.dumps(
        value,
        allow_nan=False,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")


def _normalized_distribution(value: str) -> str:
    return re.sub(r"[-_.]+", "-", value).casefold()


def _identity(namespace: str, value: object) -> str:
    return f"{namespace}:sha256:{hashlib.sha256(_canonical_json_bytes(value)).hexdigest()}"


def _canonical_relative_path(value: str) -> PurePosixPath:
    candidate = PurePosixPath(value)
    if (
        candidate.is_absolute()
        or "\\" in value
        or candidate.as_posix() != value
        or any(part in {"", ".", ".."} for part in value.split("/"))
    ):
        raise RuntimeError("bundle_manifest_path_invalid")
    return candidate


def _pe_machine_type(content: bytes) -> str:
    if len(content) < 64 or content[:2] != b"MZ":
        raise RuntimeError("wheel_native_artifact_invalid")
    pe_offset = int.from_bytes(content[60:64], "little")
    if (
        pe_offset < 64
        or pe_offset + 6 > len(content)
        or content[pe_offset : pe_offset + 4] != b"PE\0\0"
    ):
        raise RuntimeError("wheel_native_artifact_invalid")
    machine = int.from_bytes(content[pe_offset + 4 : pe_offset + 6], "little")
    machine_type = _PE_MACHINE_NAMES.get(machine)
    if machine_type is None:
        raise RuntimeError("wheel_native_architecture_mismatch")
    return machine_type


def _reviewed_source_entries(
    content: bytes,
    source: dict[str, object],
) -> dict[str, dict[str, object]]:
    document = json.loads(content)
    if not isinstance(document, dict) or set(document) != {
        "commit_id",
        "contract_version",
        "entries",
        "git_object_format",
        "run_live_environment",
        "source_manifest_identity",
        "tree_hash",
    }:
        raise RuntimeError("source_manifest_invalid")
    entries = document["entries"]
    if not isinstance(entries, list) or not 1 <= len(entries) <= 65536:
        raise RuntimeError("source_manifest_invalid")
    payload = {
        key: value for key, value in document.items() if key != "source_manifest_identity"
    }
    if (
        document["contract_version"] != source.get("contract_version")
        or document["commit_id"] != source.get("commit_id")
        or document["tree_hash"] != source.get("tree_hash")
        or document["source_manifest_identity"]
        != source.get("source_manifest_identity")
        or document["source_manifest_identity"]
        != _identity("windows-sandbox-source-manifest:v1", payload)
    ):
        raise RuntimeError("source_manifest_identity_mismatch")
    by_path: dict[str, dict[str, object]] = {}
    ordered_paths: list[str] = []
    for entry in entries:
        if not isinstance(entry, dict) or set(entry) != {
            "byte_length",
            "git_mode",
            "git_object_id",
            "relative_path",
            "sha256",
        }:
            raise RuntimeError("source_manifest_invalid")
        relative_path = str(entry["relative_path"])
        if (
            _canonical_relative_path(relative_path).as_posix() != relative_path
            or relative_path in by_path
            or entry["git_mode"] not in {"100644", "100755"}
            or re.fullmatch(r"[0-9a-f]{40,64}", str(entry["git_object_id"])) is None
            or not isinstance(entry["byte_length"], int)
            or not 0 <= entry["byte_length"] <= 1_073_741_824
            or re.fullmatch(r"[0-9a-f]{64}", str(entry["sha256"])) is None
        ):
            raise RuntimeError("source_manifest_invalid")
        ordered_paths.append(relative_path)
        by_path[relative_path] = entry
    if ordered_paths != sorted(ordered_paths):
        raise RuntimeError("source_manifest_invalid")
    return by_path


def _runtime_artifact_lock(content: bytes) -> dict[str, dict[str, object]]:
    document = json.loads(content)
    if not isinstance(document, dict) or set(document) != {
        "contract_version",
        "portable_git",
        "python",
    }:
        raise RuntimeError("artifact_lock_invalid")
    python = document["python"]
    portable_git = document["portable_git"]
    if (
        document["contract_version"] != "windows-offline-runtime-artifact-lock-v1"
        or not isinstance(python, dict)
        or set(python)
        != {
            "architecture",
            "artifact_byte_length",
            "artifact_filename",
            "artifact_sha256",
            "implementation",
            "source_url",
            "version",
        }
        or not isinstance(portable_git, dict)
        or set(portable_git)
        != {
            "archive_byte_length",
            "archive_filename",
            "archive_sha256",
            "required",
            "source_url",
            "version",
        }
    ):
        raise RuntimeError("artifact_lock_invalid")
    if (
        python["architecture"] != "AMD64"
        or python["artifact_filename"] != "python-3.13.7-amd64.exe"
        or python["implementation"] != "CPython"
        or python["version"] != "3.13.7"
        or portable_git["archive_filename"] != "MinGit-2.45.1-64-bit.zip"
        or portable_git["required"] is not True
    ):
        raise RuntimeError("artifact_lock_invalid")
    for entry, length_key, hash_key in (
        (python, "artifact_byte_length", "artifact_sha256"),
        (portable_git, "archive_byte_length", "archive_sha256"),
    ):
        if (
            not isinstance(entry[length_key], int)
            or not 1 <= entry[length_key] <= 1_073_741_824
            or re.fullmatch(r"[0-9a-f]{64}", str(entry[hash_key])) is None
        ):
            raise RuntimeError("artifact_lock_invalid")
    return {"python": python, "portable_git": portable_git}


def _project_dependency_roots(content: bytes) -> tuple[str, ...]:
    project = tomllib.loads(content.decode("utf-8"))["project"]
    optional = project["optional-dependencies"]
    roots = tuple(
        sorted(
            (
                *project["dependencies"],
                *optional["dev"],
                *optional["pit"],
                *_BOOTSTRAP_TOOL_ROOTS,
            )
        )
    )
    if not roots or any(not isinstance(root, str) or not 1 <= len(root) <= 512 for root in roots):
        raise RuntimeError("project_metadata_invalid")
    return roots


def _verify_portable_git_archive(archive_path: Path, git_root: Path) -> None:
    archived: dict[str, tuple[str, zipfile.ZipInfo]] = {}
    expanded_total = 0
    with zipfile.ZipFile(archive_path) as archive:
        for info in archive.infolist():
            if info.is_dir():
                continue
            relative = _canonical_relative_path(info.filename)
            key = relative.as_posix().casefold()
            expanded_total += info.file_size
            if (
                key in archived
                or stat.S_ISLNK(info.external_attr >> 16)
                or info.file_size > 268_435_456
                or expanded_total > 1_073_741_824
            ):
                raise RuntimeError("portable_git_archive_invalid")
            archived[key] = (relative.as_posix(), info)
        extracted = {
            path.relative_to(git_root).as_posix().casefold(): (
                path.relative_to(git_root).as_posix(),
                path,
            )
            for path in git_root.rglob("*")
            if path.is_file()
        }
        if len(extracted) != len(tuple(path for path in git_root.rglob("*") if path.is_file())):
            raise RuntimeError("portable_git_archive_invalid")
        if set(archived) != set(extracted):
            raise RuntimeError("portable_git_archive_mismatch")
        for key in sorted(archived):
            archived_name, info = archived[key]
            extracted_name, path = extracted[key]
            content = path.read_bytes()
            if (
                archived_name != extracted_name
                or len(content) != info.file_size
                or hashlib.sha256(content).digest()
                != hashlib.sha256(archive.read(info)).digest()
            ):
                raise RuntimeError("portable_git_archive_mismatch")


def verify_bundle(
    bundle_root: Path,
    expected_bundle_identity: str,
    expected_source_input_identity: str,
) -> dict[str, object]:
    manifest_path = bundle_root / "bundle-manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    if set(manifest) != _EXPECTED_MANIFEST_KEYS:
        raise RuntimeError("bundle_manifest_schema_mismatch")
    identity = manifest.pop("bundle_identity", None)
    computed = _identity("windows-offline-runtime-bundle:v1", manifest)
    if identity != expected_bundle_identity or computed != identity:
        raise RuntimeError("bundle_manifest_identity_mismatch")
    if manifest.get("contract_version") != "windows-offline-runtime-bundle-v1":
        raise RuntimeError("bundle_manifest_schema_mismatch")
    if manifest.get("supported_windows_architecture") != "AMD64":
        raise RuntimeError("bundle_target_mismatch")
    if (
        manifest.get("python_implementation") != "CPython"
        or manifest.get("python_version") != "3.13.7"
        or manifest.get("python_tag") != "cp313"
        or manifest.get("abi_tag") != "cp313"
        or manifest.get("platform_tag") != "win_amd64"
    ):
        raise RuntimeError("bundle_target_mismatch")
    if (
        manifest.get("python_installer_relative_path")
        != "python/python-3.13.7-amd64.exe"
        or manifest.get("dependency_lock_relative_path")
        != "locks/requirements-win-cp313.txt"
        or manifest.get("bootstrap_relative_path") != "bootstrap/install-runtime.ps1"
        or manifest.get("verifier_relative_path") != "bootstrap/verify_bundle.py"
        or manifest.get("portable_git_required") is not True
        or manifest.get("portable_git_relative_path") != "git/cmd/git.exe"
        or manifest.get("reviewed_source_manifest_relative_path")
        != "provenance/source-manifest.json"
        or manifest.get("runtime_artifact_lock_relative_path")
        != "provenance/iso02-artifact-lock.json"
        or manifest.get("project_metadata_relative_path")
        != "provenance/pyproject.toml"
        or manifest.get("portable_git_archive_relative_path")
        != "provenance/MinGit-2.45.1-64-bit.zip"
        or manifest.get("iso01_runtime_bundle_input_contract_version")
        != "windows-sandbox-runtime-bundle-input-v1"
    ):
        raise RuntimeError("bundle_manifest_schema_mismatch")
    source = manifest.get("source")
    if (
        not isinstance(source, dict)
        or source.get("source_input_identity") != expected_source_input_identity
    ):
        raise RuntimeError("source_bundle_binding_mismatch")

    file_rows = manifest["files"]
    inventory = manifest["wheelhouse_inventory"]
    roots = manifest["dependency_roots"]
    if (
        not isinstance(file_rows, list)
        or not 5 <= len(file_rows) <= 65536
        or not isinstance(inventory, list)
        or not 1 <= len(inventory) <= 2048
        or not isinstance(roots, list)
        or not 1 <= len(roots) <= 512
    ):
        raise RuntimeError("bundle_manifest_unbounded")
    expected: dict[str, dict[str, object]] = {}
    expected_casefolded: set[str] = set()
    for raw_entry in file_rows:
        relative_path = str(raw_entry["relative_path"])
        candidate = _canonical_relative_path(relative_path)
        casefolded_path = relative_path.casefold()
        if casefolded_path in expected_casefolded:
            raise RuntimeError("bundle_manifest_duplicate")
        expected_type = candidate.suffix.casefold().lstrip(".") or "file"
        if raw_entry.get("artifact_type") != expected_type:
            raise RuntimeError("bundle_manifest_role_mismatch")
        expected[relative_path] = raw_entry
        expected_casefolded.add(casefolded_path)
    if len(expected) != len(file_rows):
        raise RuntimeError("bundle_manifest_duplicate")
    actual = {
        path.relative_to(bundle_root).as_posix(): path
        for path in bundle_root.rglob("*")
        if path.is_file() and path.name != "bundle-manifest.json"
    }
    if set(actual) != set(expected):
        raise RuntimeError("bundle_membership_mismatch")
    for relative_path, path in sorted(actual.items()):
        entry = expected[relative_path]
        content = path.read_bytes()
        if len(content) != int(entry["byte_length"]):
            raise RuntimeError("bundle_file_length_mismatch")
        if hashlib.sha256(content).hexdigest() != str(entry["sha256"]):
            raise RuntimeError("bundle_file_hash_mismatch")

    by_name: dict[str, dict[str, object]] = {}
    for wheel in inventory:
        distribution = _normalized_distribution(str(wheel["distribution"]))
        if distribution in by_name:
            raise RuntimeError("duplicate_distribution")
        by_name[distribution] = wheel
        wheel_path = bundle_root / "wheelhouse" / str(wheel["filename"])
        with zipfile.ZipFile(wheel_path) as archive:
            native_artifacts: list[dict[str, object]] = []
            member_paths: set[str] = set()
            for info in archive.infolist():
                if info.is_dir():
                    continue
                relative = _canonical_relative_path(info.filename)
                key = relative.as_posix().casefold()
                if key in member_paths:
                    raise RuntimeError("wheel_member_duplicate")
                member_paths.add(key)
                suffix = relative.suffix.casefold()
                if suffix in _NATIVE_SUFFIXES:
                    content = archive.read(info)
                    machine_type = _pe_machine_type(content)
                    native_sha256 = hashlib.sha256(content).hexdigest()
                    if machine_type != "AMD64" and (
                        str(wheel["filename"]),
                        relative.as_posix(),
                        native_sha256,
                        machine_type,
                    ) not in _REVIEWED_CROSS_ARCH_NATIVE_EXCEPTIONS:
                        raise RuntimeError("wheel_native_architecture_mismatch")
                    native_artifacts.append(
                        {
                            "artifact_type": suffix.lstrip("."),
                            "byte_length": len(content),
                            "relative_path": relative.as_posix(),
                            "machine_type": machine_type,
                            "sha256": native_sha256,
                        }
                    )
            metadata_names = [
                name
                for name in archive.namelist()
                if name.count("/") == 1 and name.endswith(".dist-info/METADATA")
            ]
            if len(metadata_names) != 1:
                raise RuntimeError("wheel_metadata_invalid")
        native_artifacts.sort(key=lambda entry: str(entry["relative_path"]).casefold())
        if native_artifacts != wheel.get("native_artifacts"):
            raise RuntimeError("wheel_native_inventory_mismatch")

    required_roles = {
        "bootstrap/install-runtime.ps1": "bootstrap_script",
        "bootstrap/verify_bundle.py": "verifier_script",
        "git/cmd/git.exe": "portable_git_executable",
        "locks/requirements-win-cp313.txt": "dependency_lock",
        "python/python-3.13.7-amd64.exe": "python_installer",
        "provenance/MinGit-2.45.1-64-bit.zip": "portable_git_archive",
        "provenance/iso02-artifact-lock.json": "runtime_artifact_lock",
        "provenance/pyproject.toml": "project_metadata",
        "provenance/source-manifest.json": "reviewed_source_manifest",
    }
    for path, entry in expected.items():
        expected_role = required_roles.get(path)
        if expected_role is not None:
            if entry.get("role") != expected_role:
                raise RuntimeError("bundle_manifest_role_mismatch")
        elif path.startswith("wheelhouse/"):
            if entry.get("role") != "dependency_wheel":
                raise RuntimeError("bundle_manifest_role_mismatch")
        elif path.startswith("git/"):
            if entry.get("role") != "portable_git_support":
                raise RuntimeError("bundle_manifest_role_mismatch")
        else:
            raise RuntimeError("bundle_manifest_role_mismatch")
    expected_wheel_files = {f"wheelhouse/{wheel['filename']}" for wheel in inventory}
    actual_wheel_files = {
        path for path, entry in expected.items() if entry.get("role") == "dependency_wheel"
    }
    if expected_wheel_files != actual_wheel_files:
        raise RuntimeError("wheel_manifest_disagreement")
    for wheel in inventory:
        entry = expected[f"wheelhouse/{wheel['filename']}"]
        if (
            entry.get("byte_length") != wheel.get("byte_length")
            or entry.get("sha256") != wheel.get("sha256")
        ):
            raise RuntimeError("wheel_manifest_disagreement")

    wheel_payload = inventory
    if manifest["wheelhouse_inventory_identity"] != _identity(
        "windows-offline-wheelhouse:v1", wheel_payload
    ):
        raise RuntimeError("wheelhouse_inventory_identity_mismatch")
    by_path = expected
    python_entry = by_path.get(str(manifest["python_installer_relative_path"]))
    lock_entry = by_path.get(str(manifest["dependency_lock_relative_path"]))
    if python_entry is None or python_entry.get("role") != "python_installer":
        raise RuntimeError("bundle_manifest_role_mismatch")
    if lock_entry is None or lock_entry.get("role") != "dependency_lock":
        raise RuntimeError("bundle_manifest_role_mismatch")
    if manifest["python_runtime_identity"] != _identity(
        "windows-offline-python-runtime:v1", python_entry
    ):
        raise RuntimeError("python_runtime_identity_mismatch")
    if manifest["dependency_lock_identity"] != _identity(
        "windows-offline-dependency-lock:v1", lock_entry
    ):
        raise RuntimeError("dependency_lock_identity_mismatch")
    support = [
        entry
        for entry in file_rows
        if entry.get("role") in {"bootstrap_script", "verifier_script"}
    ]
    if manifest["bootstrap_support_identity"] != _identity(
        "windows-offline-bootstrap-support:v1", support
    ):
        raise RuntimeError("bootstrap_support_identity_mismatch")
    git_files = [
        entry
        for entry in file_rows
        if entry.get("role") in {"portable_git_executable", "portable_git_support"}
    ]
    if manifest["portable_git_identity"] != _identity(
        "windows-offline-portable-git:v1", git_files
    ):
        raise RuntimeError("portable_git_identity_mismatch")
    reviewed_input_paths = (
        str(manifest["reviewed_source_manifest_relative_path"]),
        str(manifest["runtime_artifact_lock_relative_path"]),
        str(manifest["project_metadata_relative_path"]),
        str(manifest["portable_git_archive_relative_path"]),
    )
    reviewed_inputs = [by_path[path] for path in reviewed_input_paths]
    if manifest["reviewed_input_identity"] != _identity(
        "windows-offline-reviewed-inputs:v1", reviewed_inputs
    ):
        raise RuntimeError("reviewed_input_identity_mismatch")

    source_entries = _reviewed_source_entries(
        (bundle_root / reviewed_input_paths[0]).read_bytes(),
        source,
    )
    for bundle_path, source_path in _TRACKED_BUNDLE_BINDINGS.items():
        source_entry = source_entries.get(source_path)
        bundle_entry = by_path[bundle_path]
        if source_entry is None or (
            source_entry["byte_length"] != bundle_entry["byte_length"]
            or source_entry["sha256"] != bundle_entry["sha256"]
        ):
            raise RuntimeError("reviewed_source_binding_mismatch")
    project_metadata = (bundle_root / reviewed_input_paths[2]).read_bytes()
    if (
        hashlib.sha256(project_metadata).hexdigest()
        != manifest["project_metadata_sha256"]
        or _project_dependency_roots(project_metadata) != tuple(roots)
    ):
        raise RuntimeError("project_metadata_mismatch")
    artifact_lock = _runtime_artifact_lock(
        (bundle_root / reviewed_input_paths[1]).read_bytes()
    )
    python_path = bundle_root / str(manifest["python_installer_relative_path"])
    python_content = python_path.read_bytes()
    if (
        python_path.name != artifact_lock["python"]["artifact_filename"]
        or len(python_content) != artifact_lock["python"]["artifact_byte_length"]
        or hashlib.sha256(python_content).hexdigest()
        != artifact_lock["python"]["artifact_sha256"]
    ):
        raise RuntimeError("python_artifact_lock_mismatch")
    portable_git_archive = bundle_root / reviewed_input_paths[3]
    archive_content = portable_git_archive.read_bytes()
    if (
        portable_git_archive.name
        != artifact_lock["portable_git"]["archive_filename"]
        or len(archive_content)
        != artifact_lock["portable_git"]["archive_byte_length"]
        or hashlib.sha256(archive_content).hexdigest()
        != artifact_lock["portable_git"]["archive_sha256"]
    ):
        raise RuntimeError("portable_git_artifact_lock_mismatch")
    _verify_portable_git_archive(portable_git_archive, bundle_root / "git")

    lock_lines = (
        bundle_root / str(manifest["dependency_lock_relative_path"])
    ).read_text(encoding="utf-8").splitlines()
    expected_lines = [
        f"{wheel['distribution']}=={wheel['version']} --hash=sha256:{wheel['sha256']}"
        for wheel in sorted(inventory, key=lambda item: str(item["distribution"]))
    ]
    if lock_lines != expected_lines:
        raise RuntimeError("dependency_lock_mismatch")
    return {
        "bundle_identity": identity,
        "contract_version": "windows-offline-python-verification-v1",
        "file_count": len(expected),
        "network_used": False,
        "wheel_count": len(inventory),
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--bundle-root", required=True, type=Path)
    parser.add_argument("--expected-bundle-identity", required=True)
    parser.add_argument("--expected-source-input-identity", required=True)
    arguments = parser.parse_args()
    result = verify_bundle(
        arguments.bundle_root,
        arguments.expected_bundle_identity,
        arguments.expected_source_input_identity,
    )
    print(json.dumps(result, allow_nan=False, separators=(",", ":"), sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

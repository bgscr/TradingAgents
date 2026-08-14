"""Content-addressed offline Windows runtime bundle contract for ISO-02.

The bundle is prepared on the host from reviewed local artifacts.  The guest
never resolves or downloads dependencies and installs only after validating
this closed manifest.
"""

from __future__ import annotations

import json
import re
import shutil
import stat
import zipfile
from collections.abc import Callable
from dataclasses import dataclass
from email.parser import Parser
from enum import Enum
from hashlib import sha256
from pathlib import Path, PurePosixPath
from typing import Literal
from urllib.parse import unquote, urlsplit

from packaging.markers import default_environment
from packaging.requirements import InvalidRequirement, Requirement
from packaging.tags import compatible_tags, cpython_tags
from packaging.utils import InvalidWheelFilename, canonicalize_name, parse_wheel_filename
from packaging.version import InvalidVersion, Version
from pydantic import BaseModel, ConfigDict, Field, model_validator

OFFLINE_RUNTIME_BUNDLE_CONTRACT_VERSION = "windows-offline-runtime-bundle-v1"
SUPPORTED_PYTHON_VERSION = "3.13.7"
SUPPORTED_WINDOWS_ARCHITECTURE = "AMD64"
SUPPORTED_PYTHON_TAG = "cp313"
SUPPORTED_ABI_TAG = "cp313"
SUPPORTED_PLATFORM_TAG = "win_amd64"
BUNDLE_MANIFEST_FILENAME = "bundle-manifest.json"
_PYTHON_INSTALLER_RELATIVE_PATH = f"python/python-{SUPPORTED_PYTHON_VERSION}-amd64.exe"
_DEPENDENCY_LOCK_RELATIVE_PATH = "locks/requirements-win-cp313.txt"
_BOOTSTRAP_RELATIVE_PATH = "bootstrap/install-runtime.ps1"
_VERIFIER_RELATIVE_PATH = "bootstrap/verify_bundle.py"
_PORTABLE_GIT_RELATIVE_PATH = "git/cmd/git.exe"
_SOURCE_MANIFEST_RELATIVE_PATH = "provenance/source-manifest.json"
_ARTIFACT_LOCK_RELATIVE_PATH = "provenance/iso02-artifact-lock.json"
_PROJECT_METADATA_RELATIVE_PATH = "provenance/pyproject.toml"
_PORTABLE_GIT_ARCHIVE_RELATIVE_PATH = "provenance/MinGit-2.45.1-64-bit.zip"
_BOOTSTRAP_TOOL_ROOTS = ("pip==25.2", "wheel==0.46.3")
_TRACKED_BUNDLE_BINDINGS = {
    _ARTIFACT_LOCK_RELATIVE_PATH: (
        "scripts/windows_sandbox/iso02-artifact-lock.json"
    ),
    _PROJECT_METADATA_RELATIVE_PATH: "pyproject.toml",
    _BOOTSTRAP_RELATIVE_PATH: (
        "scripts/windows_sandbox/offline_bundle_bootstrap.ps1"
    ),
    _VERIFIER_RELATIVE_PATH: "scripts/windows_sandbox/verify_offline_bundle.py",
    _DEPENDENCY_LOCK_RELATIVE_PATH: (
        "scripts/windows_sandbox/iso02-requirements-win-cp313.txt"
    ),
}
_GUEST_WHEELHOUSE = r"C:\QCF17\Input\Runtime\wheelhouse"
_GUEST_DEPENDENCY_LOCK = r"C:\QCF17\Input\Runtime\locks\requirements-win-cp313.txt"
_GUEST_PROJECT_SOURCE = r"C:\QCF17\Work\Repository"
_DEPENDENCY_INSTALL_ARGUMENTS = (
    "-m",
    "pip",
    "--isolated",
    "install",
    "--no-index",
    "--require-hashes",
    "--find-links",
    _GUEST_WHEELHOUSE,
    "-r",
    _GUEST_DEPENDENCY_LOCK,
)
_PROJECT_INSTALL_ARGUMENTS = (
    "-m",
    "pip",
    "--isolated",
    "install",
    "--no-index",
    "--no-deps",
    "--no-build-isolation",
    _GUEST_PROJECT_SOURCE,
)
_CLOSED_MODEL_CONFIG = ConfigDict(frozen=True, extra="forbid")
_WINDOWS_REPARSE_POINT_ATTRIBUTE = 0x400
_CREDENTIAL_URL_PATTERN = re.compile(
    r"(?i)https?://[^\s/:@]+:[^\s/@]+@[^\s/'\"?#]+"
)
_KNOWN_SECRET_PATTERN = re.compile(
    r"(?:\bAKIA[0-9A-Z]{16}\b|\bgh[oprsu]_[A-Za-z0-9]{20,}\b|"
    r"\bsk-[A-Za-z0-9_-]{20,}\b)"
)
_PRIVATE_KEY_BLOCK_PATTERN = re.compile(
    r"-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----\s+"
    r"[A-Za-z0-9+/=\r\n]{64,}\s+"
    r"-----END (?:RSA |EC |OPENSSH )?PRIVATE KEY-----"
)
_PACKAGE_TOKEN_ASSIGNMENT = re.compile(
    r"(?im)^\s*(?:PIP_INDEX_URL|PIP_EXTRA_INDEX_URL|POETRY_HTTP_BASIC_[A-Z0-9_]+)"
    r"\s*[:=]\s*\S+"
)
_HOST_ABSOLUTE_PATH_PATTERN = re.compile(
    r"(?i)\b[A-Z]:[\\/](?:Users|Documents and Settings)[\\/]"
)
_NATIVE_WHEEL_SUFFIXES = frozenset({".dll", ".exe", ".pyd"})
_REVIEWED_WHEEL_TEXT_EXCEPTIONS = frozenset(
    {
        (
            "numpy-2.5.2-cp313-cp313-win_amd64.whl",
            "numpy/__config__.py",
            "ba1b10cc856f4b86b2043f9b720b8c09f608e9dfeda9aef14b6ee2c5da9a3a50",
        ),
        (
            "pip-25.2-py3-none-any.whl",
            "pip/_internal/utils/urls.py",
            "685fde83dba5e5df1b302c5f4924b141c7ecf8ea4975b4ad62a647a32d8ad511",
        ),
        (
            "platformdirs-4.11.3-py3-none-any.whl",
            "platformdirs/windows.py",
            "7a8f8ad7e204a0bbc646a913680bc6199d94f152ebdafe3e2e6235f6b7aab985",
        ),
        (
            "pydantic-2.13.4-py3-none-any.whl",
            "pydantic/networks.py",
            "80207dea0b741bbb625438559c97e92abd4044be6a907f923d992e364986d8ee",
        ),
        (
            "setuptools-84.0.0-py3-none-any.whl",
            "setuptools/_vendor/platformdirs-4.4.0.dist-info/METADATA",
            "bbc5216d5f4c77bfbc57227264d52e66bccde71ccf79e75e1a6046d029d30223",
        ),
        (
            "typer-0.27.1-py3-none-any.whl",
            "typer/_click/utils.py",
            "d9022cc7ad48a10c2045d3c00718d7f9320fe4b84baf51d419c375155d4cb934",
        ),
        (
            "websocket_client-1.9.0-py3-none-any.whl",
            "websocket/tests/test_url.py",
            "c305fdd91a0363479d0cac320ff3addeb2b6e25c6d8d9e38f22d580d632aaadb",
        ),
    }
)
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


def _canonical_json_bytes(value: object) -> bytes:
    return json.dumps(
        value,
        allow_nan=False,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")


def _identity(namespace: str, value: object) -> str:
    return f"{namespace}:sha256:{sha256(_canonical_json_bytes(value)).hexdigest()}"


class OfflineBundleFailureReason(str, Enum):
    MANIFEST_INVALID = "bundle_manifest_invalid"
    MANIFEST_IDENTITY_MISMATCH = "bundle_manifest_identity_mismatch"
    SOURCE_BINDING_MISMATCH = "source_binding_mismatch"
    FILE_MISSING = "bundle_file_missing"
    FILE_EXTRA = "bundle_file_extra"
    FILE_LENGTH_MISMATCH = "bundle_file_length_mismatch"
    FILE_HASH_MISMATCH = "bundle_file_hash_mismatch"
    REPARSE_POINT = "bundle_reparse_point"
    WHEEL_INVALID = "wheel_invalid"
    WRONG_PYTHON_ABI = "wrong_python_abi"
    WRONG_ARCHITECTURE = "wrong_architecture"
    WRONG_PLATFORM = "wrong_wheel_platform"
    DUPLICATE_DISTRIBUTION = "duplicate_distribution"
    ROOT_DEPENDENCY_MISSING = "root_dependency_missing"
    MISSING_TRANSITIVE_DEPENDENCY = "missing_transitive_dependency"
    DEPENDENCY_VERSION_MISMATCH = "dependency_version_mismatch"
    UNDECLARED_DEPENDENCY = "undeclared_dependency"
    DEPENDENCY_LOCK_MISMATCH = "dependency_lock_mismatch"
    FORBIDDEN_INPUT = "forbidden_bundle_input"
    CREDENTIAL_INPUT = "credential_bundle_input"


class OfflineBundleContractError(ValueError):
    """Closed, value-free refusal for bundle construction or verification."""

    def __init__(self, reason: OfflineBundleFailureReason) -> None:
        self.reason = reason
        super().__init__(reason.value)


class ProvisioningCommandRole(str, Enum):
    INSTALL_PYTHON = "install_python"
    CREATE_VIRTUAL_ENVIRONMENT = "create_virtual_environment"
    VERIFY_WHEELHOUSE = "verify_wheelhouse"
    INSTALL_DEPENDENCIES = "install_dependencies"
    INSTALL_PROJECT = "install_project"


class OfflineProvisioningCommand(BaseModel):
    model_config = _CLOSED_MODEL_CONFIG

    role: ProvisioningCommandRole
    argv: tuple[str, ...] = Field(min_length=2, max_length=32)

    @model_validator(mode="after")
    def _validate_argv(self) -> OfflineProvisioningCommand:
        if any(not value or len(value) > 512 for value in self.argv):
            raise ValueError("offline provisioning argument is unbounded")
        return self


class OfflineProvisioningPlan(BaseModel):
    model_config = _CLOSED_MODEL_CONFIG

    contract_version: Literal["windows-offline-provisioning-plan-v1"] = (
        "windows-offline-provisioning-plan-v1"
    )
    bundle_identity: str = Field(
        pattern=r"^windows-offline-runtime-bundle:v1:sha256:[0-9a-f]{64}$"
    )
    verification_required_before_execution: Literal[True] = True
    network_permitted: Literal[False] = False
    host_python_permitted: Literal[False] = False
    environment: tuple[tuple[str, str], ...] = Field(min_length=1, max_length=16)
    commands: tuple[OfflineProvisioningCommand, ...] = Field(min_length=1, max_length=8)
    plan_identity: str = Field(
        pattern=r"^windows-offline-provisioning-plan:v1:sha256:[0-9a-f]{64}$"
    )

    @model_validator(mode="after")
    def _validate_plan(self) -> OfflineProvisioningPlan:
        if tuple(command.role for command in self.commands) != tuple(ProvisioningCommandRole):
            raise ValueError("offline provisioning command order is closed")
        if self.environment != tuple(sorted(self.environment)):
            raise ValueError("offline provisioning environment must be canonical")
        if any(
            not name
            or len(name) > 80
            or len(value) > 512
            for name, value in self.environment
        ):
            raise ValueError("offline provisioning environment is unbounded")
        if any(
            item.casefold().startswith(("http://", "https://"))
            for command in self.commands
            for item in command.argv
        ):
            raise ValueError("offline provisioning command contains a network location")
        payload = self.model_dump(mode="json", exclude={"plan_identity"})
        if self.plan_identity != _identity("windows-offline-provisioning-plan:v1", payload):
            raise ValueError("offline provisioning plan identity mismatch")
        return self


class OfflineProvisioningEvidence(BaseModel):
    model_config = _CLOSED_MODEL_CONFIG

    contract_version: Literal["windows-offline-provisioning-evidence-v1"] = (
        "windows-offline-provisioning-evidence-v1"
    )
    bundle_identity: str = Field(
        pattern=r"^windows-offline-runtime-bundle:v1:sha256:[0-9a-f]{64}$"
    )
    plan_identity: str = Field(
        pattern=r"^windows-offline-provisioning-plan:v1:sha256:[0-9a-f]{64}$"
    )
    status: Literal["provisioned"] = "provisioned"
    completed_roles: tuple[ProvisioningCommandRole, ...] = Field(min_length=1, max_length=8)
    network_used: Literal[False] = False
    host_python_used: Literal[False] = False
    evidence_identity: str = Field(
        pattern=r"^windows-offline-provisioning-evidence:v1:sha256:[0-9a-f]{64}$"
    )

    @model_validator(mode="after")
    def _validate_evidence(self) -> OfflineProvisioningEvidence:
        if self.completed_roles != tuple(ProvisioningCommandRole):
            raise ValueError("offline provisioning evidence is incomplete")
        payload = self.model_dump(mode="json", exclude={"evidence_identity"})
        if self.evidence_identity != _identity(
            "windows-offline-provisioning-evidence:v1", payload
        ):
            raise ValueError("offline provisioning evidence identity mismatch")
        return self


class OfflineBundleSourceBinding(BaseModel):
    """The exact ISO-01 reviewed-source identity consumed by this bundle."""

    model_config = _CLOSED_MODEL_CONFIG

    contract_version: Literal["windows-sandbox-reviewed-source-v1"] = (
        "windows-sandbox-reviewed-source-v1"
    )
    commit_id: str = Field(pattern=r"^[0-9a-f]{40,64}$")
    tree_hash: str = Field(pattern=r"^[0-9a-f]{40,64}$")
    source_input_identity: str = Field(
        pattern=r"^windows-sandbox-reviewed-source:v1:sha256:[0-9a-f]{64}$"
    )
    source_manifest_identity: str = Field(
        pattern=r"^windows-sandbox-source-manifest:v1:sha256:[0-9a-f]{64}$"
    )
    source_archive_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")


class BundleArtifactRole(str, Enum):
    PYTHON_INSTALLER = "python_installer"
    DEPENDENCY_WHEEL = "dependency_wheel"
    DEPENDENCY_LOCK = "dependency_lock"
    BOOTSTRAP_SCRIPT = "bootstrap_script"
    VERIFIER_SCRIPT = "verifier_script"
    PORTABLE_GIT_EXECUTABLE = "portable_git_executable"
    PORTABLE_GIT_SUPPORT = "portable_git_support"
    REVIEWED_SOURCE_MANIFEST = "reviewed_source_manifest"
    RUNTIME_ARTIFACT_LOCK = "runtime_artifact_lock"
    PROJECT_METADATA = "project_metadata"
    PORTABLE_GIT_ARCHIVE = "portable_git_archive"


class BundleFileEntry(BaseModel):
    model_config = _CLOSED_MODEL_CONFIG

    relative_path: str = Field(min_length=1, max_length=512)
    byte_length: int = Field(ge=1, le=1_073_741_824)
    sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    role: BundleArtifactRole
    artifact_type: str = Field(min_length=1, max_length=64)

    @model_validator(mode="after")
    def _validate_entry(self) -> BundleFileEntry:
        candidate = PurePosixPath(self.relative_path)
        if (
            candidate.is_absolute()
            or "\\" in self.relative_path
            or candidate.as_posix() != self.relative_path
            or any(part in {"", ".", ".."} for part in self.relative_path.split("/"))
        ):
            raise ValueError("bundle file path must be canonical and relative")
        expected_type = candidate.suffix.casefold().lstrip(".") or "file"
        if self.artifact_type != expected_type:
            raise ValueError("bundle file artifact type mismatch")
        return self


class WheelArchiveArtifact(BaseModel):
    model_config = _CLOSED_MODEL_CONFIG

    relative_path: str = Field(min_length=1, max_length=512)
    byte_length: int = Field(ge=0, le=1_073_741_824)
    sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    artifact_type: Literal["dll", "exe", "pyd"]
    machine_type: Literal["AMD64", "ARM64", "I386"]

    @model_validator(mode="after")
    def _validate_native_artifact(self) -> WheelArchiveArtifact:
        candidate = _validated_archive_member_path(self.relative_path)
        if candidate.suffix.casefold().lstrip(".") != self.artifact_type:
            raise ValueError("native wheel artifact type mismatch")
        return self


class WheelInventoryEntry(BaseModel):
    model_config = _CLOSED_MODEL_CONFIG

    filename: str = Field(min_length=1, max_length=260)
    distribution: str = Field(min_length=1, max_length=120)
    version: str = Field(min_length=1, max_length=80)
    python_tag: str = Field(min_length=1, max_length=80)
    abi_tag: str = Field(min_length=1, max_length=80)
    platform_tag: str = Field(min_length=1, max_length=80)
    byte_length: int = Field(ge=1, le=1_073_741_824)
    sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    requires_dist: tuple[str, ...] = Field(default=(), max_length=512)
    native_artifacts: tuple[WheelArchiveArtifact, ...] = Field(default=(), max_length=4096)

    @model_validator(mode="after")
    def _validate_inventory_entry(self) -> WheelInventoryEntry:
        if self.distribution != _normalize_distribution(self.distribution):
            raise ValueError("wheel distribution must be canonical")
        if any(not value or len(value) > 512 for value in self.requires_dist):
            raise ValueError("wheel dependency metadata is unbounded")
        paths = tuple(artifact.relative_path for artifact in self.native_artifacts)
        if paths != tuple(sorted(paths, key=str.casefold)) or len(paths) != len(
            {path.casefold() for path in paths}
        ):
            raise ValueError("native wheel artifact inventory must be canonical")
        for artifact in self.native_artifacts:
            if artifact.machine_type == "AMD64":
                continue
            exception = (
                self.filename,
                artifact.relative_path,
                artifact.sha256,
                artifact.machine_type,
            )
            if exception not in _REVIEWED_CROSS_ARCH_NATIVE_EXCEPTIONS:
                raise ValueError("native wheel artifact architecture mismatch")
        return self


class OfflineInstallPolicy(BaseModel):
    model_config = _CLOSED_MODEL_CONFIG

    network_permitted: Literal[False] = False
    host_python_permitted: Literal[False] = False
    host_virtual_environment_permitted: Literal[False] = False
    dependency_install_arguments: tuple[str, ...] = Field(min_length=1, max_length=16)
    project_install_arguments: tuple[str, ...] = Field(min_length=1, max_length=16)

    @model_validator(mode="after")
    def _validate_closed_install(self) -> OfflineInstallPolicy:
        if self.dependency_install_arguments != _DEPENDENCY_INSTALL_ARGUMENTS:
            raise ValueError("dependency install must be exactly hash-locked and offline")
        if self.project_install_arguments != _PROJECT_INSTALL_ARGUMENTS:
            raise ValueError("project install must exactly disable dependency resolution")
        return self


class OfflineRuntimeBundleManifest(BaseModel):
    model_config = _CLOSED_MODEL_CONFIG

    contract_version: Literal["windows-offline-runtime-bundle-v1"] = (
        OFFLINE_RUNTIME_BUNDLE_CONTRACT_VERSION
    )
    bundle_identity: str = Field(
        pattern=r"^windows-offline-runtime-bundle:v1:sha256:[0-9a-f]{64}$"
    )
    source: OfflineBundleSourceBinding
    supported_windows_architecture: Literal["AMD64"]
    python_implementation: Literal["CPython"] = "CPython"
    python_version: Literal["3.13.7"]
    python_tag: Literal["cp313"]
    abi_tag: Literal["cp313"]
    platform_tag: Literal["win_amd64"]
    python_installer_relative_path: Literal[
        "python/python-3.13.7-amd64.exe"
    ]
    python_runtime_identity: str = Field(
        pattern=r"^windows-offline-python-runtime:v1:sha256:[0-9a-f]{64}$"
    )
    dependency_roots: tuple[str, ...] = Field(min_length=1, max_length=512)
    dependency_lock_relative_path: Literal["locks/requirements-win-cp313.txt"]
    dependency_lock_identity: str = Field(
        pattern=r"^windows-offline-dependency-lock:v1:sha256:[0-9a-f]{64}$"
    )
    wheelhouse_inventory: tuple[WheelInventoryEntry, ...] = Field(
        min_length=1,
        max_length=2048,
    )
    wheelhouse_inventory_identity: str = Field(
        pattern=r"^windows-offline-wheelhouse:v1:sha256:[0-9a-f]{64}$"
    )
    bootstrap_relative_path: Literal["bootstrap/install-runtime.ps1"]
    verifier_relative_path: Literal["bootstrap/verify_bundle.py"]
    bootstrap_support_identity: str = Field(
        pattern=r"^windows-offline-bootstrap-support:v1:sha256:[0-9a-f]{64}$"
    )
    portable_git_required: Literal[True]
    portable_git_relative_path: Literal["git/cmd/git.exe"]
    portable_git_identity: str = Field(
        pattern=r"^windows-offline-portable-git:v1:sha256:[0-9a-f]{64}$"
    )
    reviewed_source_manifest_relative_path: Literal[
        "provenance/source-manifest.json"
    ]
    runtime_artifact_lock_relative_path: Literal[
        "provenance/iso02-artifact-lock.json"
    ]
    project_metadata_relative_path: Literal["provenance/pyproject.toml"]
    portable_git_archive_relative_path: Literal[
        "provenance/MinGit-2.45.1-64-bit.zip"
    ]
    reviewed_input_identity: str = Field(
        pattern=r"^windows-offline-reviewed-inputs:v1:sha256:[0-9a-f]{64}$"
    )
    project_metadata_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    iso01_runtime_bundle_input_contract_version: Literal[
        "windows-sandbox-runtime-bundle-input-v1"
    ] = (
        "windows-sandbox-runtime-bundle-input-v1"
    )
    files: tuple[BundleFileEntry, ...] = Field(min_length=5, max_length=65536)
    install_policy: OfflineInstallPolicy

    @model_validator(mode="after")
    def _validate_manifest(self) -> OfflineRuntimeBundleManifest:
        paths = tuple(entry.relative_path for entry in self.files)
        if paths != tuple(sorted(paths, key=str.casefold)) or len(paths) != len(
            {path.casefold() for path in paths}
        ):
            raise ValueError("bundle files must be sorted and case-unique")
        wheels = tuple(entry.filename for entry in self.wheelhouse_inventory)
        if wheels != tuple(sorted(wheels, key=str.casefold)):
            raise ValueError("wheel inventory must use canonical ordering")
        distributions = tuple(entry.distribution for entry in self.wheelhouse_inventory)
        if len(distributions) != len(set(distributions)):
            raise ValueError("wheel inventory distributions must be unique")
        if self.dependency_roots != tuple(sorted(self.dependency_roots)) or any(
            not root or len(root) > 512 for root in self.dependency_roots
        ):
            raise ValueError("dependency roots must be canonical and bounded")

        by_path = {entry.relative_path: entry for entry in self.files}
        required_roles = {
            self.python_installer_relative_path: BundleArtifactRole.PYTHON_INSTALLER,
            self.dependency_lock_relative_path: BundleArtifactRole.DEPENDENCY_LOCK,
            self.bootstrap_relative_path: BundleArtifactRole.BOOTSTRAP_SCRIPT,
            self.verifier_relative_path: BundleArtifactRole.VERIFIER_SCRIPT,
            self.portable_git_relative_path: BundleArtifactRole.PORTABLE_GIT_EXECUTABLE,
            self.reviewed_source_manifest_relative_path: (
                BundleArtifactRole.REVIEWED_SOURCE_MANIFEST
            ),
            self.runtime_artifact_lock_relative_path: (
                BundleArtifactRole.RUNTIME_ARTIFACT_LOCK
            ),
            self.project_metadata_relative_path: BundleArtifactRole.PROJECT_METADATA,
            self.portable_git_archive_relative_path: (
                BundleArtifactRole.PORTABLE_GIT_ARCHIVE
            ),
        }
        if any(
            path not in by_path or by_path[path].role is not role
            for path, role in required_roles.items()
        ):
            raise ValueError("bundle manifest required artifact role mismatch")
        for entry in self.files:
            expected_role = required_roles.get(entry.relative_path)
            if expected_role is not None:
                if entry.role is not expected_role:
                    raise ValueError("bundle manifest required artifact role mismatch")
            elif entry.relative_path.startswith("wheelhouse/"):
                if entry.role is not BundleArtifactRole.DEPENDENCY_WHEEL:
                    raise ValueError("bundle manifest wheel role mismatch")
            elif entry.relative_path.startswith("git/"):
                if entry.role is not BundleArtifactRole.PORTABLE_GIT_SUPPORT:
                    raise ValueError("bundle manifest portable Git role mismatch")
            else:
                raise ValueError("bundle manifest contains an unexpected artifact path")

        wheel_files = {
            entry.relative_path: entry
            for entry in self.files
            if entry.role is BundleArtifactRole.DEPENDENCY_WHEEL
        }
        expected_wheel_paths = {
            f"wheelhouse/{wheel.filename}" for wheel in self.wheelhouse_inventory
        }
        if set(wheel_files) != expected_wheel_paths:
            raise ValueError("wheel inventory and bundle files disagree")
        for wheel in self.wheelhouse_inventory:
            file_entry = wheel_files[f"wheelhouse/{wheel.filename}"]
            if (
                file_entry.byte_length != wheel.byte_length
                or file_entry.sha256 != wheel.sha256
            ):
                raise ValueError("wheel inventory content identity mismatch")

        python_entry = by_path[self.python_installer_relative_path]
        if self.python_runtime_identity != _identity(
            "windows-offline-python-runtime:v1",
            python_entry.model_dump(mode="json"),
        ):
            raise ValueError("Python runtime artifact identity mismatch")
        lock_entry = by_path[self.dependency_lock_relative_path]
        if self.dependency_lock_identity != _identity(
            "windows-offline-dependency-lock:v1",
            lock_entry.model_dump(mode="json"),
        ):
            raise ValueError("dependency lock artifact identity mismatch")
        wheel_payload = [
            wheel.model_dump(mode="json") for wheel in self.wheelhouse_inventory
        ]
        if self.wheelhouse_inventory_identity != _identity(
            "windows-offline-wheelhouse:v1",
            wheel_payload,
        ):
            raise ValueError("wheelhouse inventory identity mismatch")
        support_payload = [
            entry.model_dump(mode="json")
            for entry in self.files
            if entry.role
            in {BundleArtifactRole.BOOTSTRAP_SCRIPT, BundleArtifactRole.VERIFIER_SCRIPT}
        ]
        if self.bootstrap_support_identity != _identity(
            "windows-offline-bootstrap-support:v1",
            support_payload,
        ):
            raise ValueError("bootstrap support identity mismatch")
        git_payload = [
            entry.model_dump(mode="json")
            for entry in self.files
            if entry.role
            in {
                BundleArtifactRole.PORTABLE_GIT_EXECUTABLE,
                BundleArtifactRole.PORTABLE_GIT_SUPPORT,
            }
        ]
        if self.portable_git_identity != _identity(
            "windows-offline-portable-git:v1",
            git_payload,
        ):
            raise ValueError("portable Git identity mismatch")
        reviewed_input_payload = [
            by_path[self.reviewed_source_manifest_relative_path].model_dump(mode="json"),
            by_path[self.runtime_artifact_lock_relative_path].model_dump(mode="json"),
            by_path[self.project_metadata_relative_path].model_dump(mode="json"),
            by_path[self.portable_git_archive_relative_path].model_dump(mode="json"),
        ]
        if self.reviewed_input_identity != _identity(
            "windows-offline-reviewed-inputs:v1",
            reviewed_input_payload,
        ):
            raise ValueError("reviewed input identity mismatch")
        payload = self.model_dump(mode="json", exclude={"bundle_identity"})
        if self.bundle_identity != _identity("windows-offline-runtime-bundle:v1", payload):
            raise ValueError("offline runtime bundle identity mismatch")
        return self

    def canonical_json_bytes(self) -> bytes:
        return _canonical_json_bytes(self.model_dump(mode="json"))

    def canonical_json(self) -> str:
        return self.canonical_json_bytes().decode("utf-8")


@dataclass(frozen=True, slots=True)
class OfflineRuntimeBundleBuildRequest:
    destination: Path
    source: OfflineBundleSourceBinding
    python_installer: Path
    python_version: str
    windows_architecture: str
    wheelhouse: Path
    dependency_roots: tuple[str, ...]
    project_metadata_sha256: str
    bootstrap_script: Path
    verifier_script: Path
    portable_git_root: Path
    reviewed_source_manifest: Path
    runtime_artifact_lock: Path
    project_metadata: Path
    portable_git_archive: Path
    expected_dependency_lock: Path


@dataclass(frozen=True, slots=True)
class BuiltOfflineRuntimeBundle:
    root: Path
    manifest_path: Path
    manifest: OfflineRuntimeBundleManifest
    manifest_sha256: str


def _file_entry(path: Path, relative_path: str, role: BundleArtifactRole) -> BundleFileEntry:
    content = path.read_bytes()
    return BundleFileEntry(
        relative_path=relative_path,
        byte_length=len(content),
        sha256=sha256(content).hexdigest(),
        role=role,
        artifact_type=path.suffix.lower().lstrip(".") or "file",
    )


def _normalize_distribution(value: str) -> str:
    return str(canonicalize_name(value))


def _forbidden_member_name(relative_path: str) -> bool:
    normalized = relative_path.replace("\\", "/").casefold()
    parts = tuple(part for part in normalized.split("/") if part)
    name = parts[-1] if parts else ""
    if name in {".env", ".env.enterprise"}:
        return True
    if name.startswith(".env.") and not name.endswith((".example", ".sample", ".template")):
        return True
    if name in {
        ".netrc",
        ".pypirc",
        "pip.ini",
        "cookies.sqlite",
        "login data",
        "credentials.json",
        "credential-store.json",
    }:
        return True
    if any(part in {".aws", ".azure", ".kube", "gcloud", ".tradingagents"} for part in parts):
        return True
    database_name = name.removesuffix("-wal").removesuffix("-shm")
    if database_name.endswith((".db", ".sqlite", ".sqlite3")):
        return True
    return name.endswith((".cookie", ".cookies", ".credentials"))


def _is_placeholder_secret(value: str) -> bool:
    if value.startswith("AKIA"):
        payload = value[4:]
    elif value.startswith("sk-"):
        payload = value[3:]
    else:
        payload = value.partition("_")[2]
    return bool(payload) and not (set(payload.casefold()) - {"x", "0"})


def _validate_safe_content(
    relative_path: str,
    content: bytes,
    *,
    reject_credential_urls: bool = True,
    reject_host_paths: bool = True,
) -> None:
    if _forbidden_member_name(relative_path):
        raise OfflineBundleContractError(OfflineBundleFailureReason.FORBIDDEN_INPUT)
    if b"\0" in content[:8192]:
        return
    text = content.decode("utf-8", errors="ignore")
    credential_urls = tuple(_CREDENTIAL_URL_PATTERN.finditer(text))
    if reject_credential_urls and credential_urls and not all(
        unquote((parsed := urlsplit(match.group(0))).username or "")
        in {"jo@email.com", "user", "username"}
        and unquote(parsed.password or "") in {"a secret", "pass", "password"}
        for match in credential_urls
    ):
        raise OfflineBundleContractError(OfflineBundleFailureReason.CREDENTIAL_INPUT)
    secret_matches = tuple(_KNOWN_SECRET_PATTERN.finditer(text))
    non_placeholder_secret = any(
        not _is_placeholder_secret(match.group(0)) for match in secret_matches
    )
    if (
        non_placeholder_secret
        or _PRIVATE_KEY_BLOCK_PATTERN.search(text)
        or _PACKAGE_TOKEN_ASSIGNMENT.search(text)
    ):
        raise OfflineBundleContractError(OfflineBundleFailureReason.CREDENTIAL_INPUT)
    if reject_host_paths and _HOST_ABSOLUTE_PATH_PATTERN.search(text):
        raise OfflineBundleContractError(OfflineBundleFailureReason.FORBIDDEN_INPUT)


def _validate_regular_input(path: Path, *, relative_path: str) -> None:
    try:
        metadata = path.lstat()
        if (
            not stat.S_ISREG(metadata.st_mode)
            or stat.S_ISLNK(metadata.st_mode)
            or bool(
                getattr(metadata, "st_file_attributes", 0)
                & _WINDOWS_REPARSE_POINT_ATTRIBUTE
            )
        ):
            raise OfflineBundleContractError(OfflineBundleFailureReason.REPARSE_POINT)
        _validate_safe_content(relative_path, path.read_bytes())
    except OfflineBundleContractError:
        raise
    except OSError as exc:
        raise OfflineBundleContractError(OfflineBundleFailureReason.FORBIDDEN_INPUT) from exc


def _validate_directory_root(path: Path) -> None:
    try:
        metadata = path.lstat()
    except OSError as exc:
        raise OfflineBundleContractError(OfflineBundleFailureReason.FORBIDDEN_INPUT) from exc
    if (
        not stat.S_ISDIR(metadata.st_mode)
        or stat.S_ISLNK(metadata.st_mode)
        or bool(
            getattr(metadata, "st_file_attributes", 0)
            & _WINDOWS_REPARSE_POINT_ATTRIBUTE
        )
    ):
        raise OfflineBundleContractError(OfflineBundleFailureReason.REPARSE_POINT)


def _target_tags() -> frozenset[str]:
    tags = {
        *cpython_tags(
            python_version=(3, 13),
            abis=(SUPPORTED_ABI_TAG, "abi3", "none"),
            platforms=(SUPPORTED_PLATFORM_TAG,),
        ),
        *compatible_tags(
            python_version=(3, 13),
            interpreter=SUPPORTED_PYTHON_TAG,
            platforms=(SUPPORTED_PLATFORM_TAG,),
        ),
    }
    return frozenset(str(tag) for tag in tags)


_SUPPORTED_WHEEL_TAGS = _target_tags()


def _validated_archive_member_path(filename: str) -> PurePosixPath:
    raw_parts = filename.split("/")
    candidate = PurePosixPath(filename)
    if (
        candidate.is_absolute()
        or "\\" in filename
        or any(part in {"", ".", ".."} for part in raw_parts)
        or candidate.as_posix() != filename
    ):
        raise OfflineBundleContractError(OfflineBundleFailureReason.WHEEL_INVALID)
    return candidate


def _pe_machine_type(content: bytes) -> str:
    if len(content) < 64 or content[:2] != b"MZ":
        raise OfflineBundleContractError(OfflineBundleFailureReason.WHEEL_INVALID)
    pe_offset = int.from_bytes(content[60:64], "little")
    if pe_offset < 64 or pe_offset + 6 > len(content):
        raise OfflineBundleContractError(OfflineBundleFailureReason.WHEEL_INVALID)
    if content[pe_offset : pe_offset + 4] != b"PE\0\0":
        raise OfflineBundleContractError(OfflineBundleFailureReason.WHEEL_INVALID)
    machine = int.from_bytes(content[pe_offset + 4 : pe_offset + 6], "little")
    machine_type = _PE_MACHINE_NAMES.get(machine)
    if machine_type is None:
        raise OfflineBundleContractError(OfflineBundleFailureReason.WRONG_ARCHITECTURE)
    return machine_type


def _read_wheel(path: Path) -> WheelInventoryEntry:
    try:
        parsed_distribution, parsed_version, _build, tags = parse_wheel_filename(path.name)
    except InvalidWheelFilename as exc:
        raise OfflineBundleContractError(OfflineBundleFailureReason.WHEEL_INVALID) from exc
    rendered_tags = frozenset(str(tag) for tag in tags)
    if not rendered_tags & _SUPPORTED_WHEEL_TAGS:
        platforms = {tag.platform for tag in tags}
        interpreters = {tag.interpreter for tag in tags}
        abis = {tag.abi for tag in tags}
        if any(platform.startswith("win_") for platform in platforms):
            if SUPPORTED_PLATFORM_TAG not in platforms:
                reason = OfflineBundleFailureReason.WRONG_ARCHITECTURE
            elif SUPPORTED_PYTHON_TAG not in interpreters or not (
                {SUPPORTED_ABI_TAG, "abi3", "none"} & abis
            ):
                reason = OfflineBundleFailureReason.WRONG_PYTHON_ABI
            else:
                reason = OfflineBundleFailureReason.WRONG_PLATFORM
        elif platforms != {"any"}:
            reason = OfflineBundleFailureReason.WRONG_PLATFORM
        else:
            reason = OfflineBundleFailureReason.WRONG_PYTHON_ABI
        raise OfflineBundleContractError(reason)
    try:
        with zipfile.ZipFile(path) as archive:
            names = archive.namelist()
            normalized_names: set[str] = set()
            native_artifacts: list[WheelArchiveArtifact] = []
            for info in archive.infolist():
                if info.is_dir():
                    directory_name = info.filename.removesuffix("/")
                    if directory_name:
                        _validated_archive_member_path(directory_name)
                    continue
                member = _validated_archive_member_path(info.filename)
                normalized_name = member.as_posix().casefold()
                unix_mode = info.external_attr >> 16
                if normalized_name in normalized_names or stat.S_ISLNK(unix_mode):
                    raise OfflineBundleContractError(OfflineBundleFailureReason.WHEEL_INVALID)
                normalized_names.add(normalized_name)
                if _forbidden_member_name(info.filename):
                    raise OfflineBundleContractError(OfflineBundleFailureReason.FORBIDDEN_INPUT)
                content = archive.read(info)
                try:
                    _validate_safe_content(info.filename, content)
                except OfflineBundleContractError:
                    exception_identity = (
                        path.name,
                        info.filename,
                        sha256(content).hexdigest(),
                    )
                    if exception_identity not in _REVIEWED_WHEEL_TEXT_EXCEPTIONS:
                        raise
                suffix = member.suffix.casefold()
                if content.startswith(b"MZ") and suffix not in _NATIVE_WHEEL_SUFFIXES:
                    raise OfflineBundleContractError(OfflineBundleFailureReason.WHEEL_INVALID)
                if suffix in _NATIVE_WHEEL_SUFFIXES:
                    machine_type = _pe_machine_type(content)
                    native_sha256 = sha256(content).hexdigest()
                    if machine_type != "AMD64" and (
                        path.name,
                        member.as_posix(),
                        native_sha256,
                        machine_type,
                    ) not in _REVIEWED_CROSS_ARCH_NATIVE_EXCEPTIONS:
                        raise OfflineBundleContractError(
                            OfflineBundleFailureReason.WRONG_ARCHITECTURE
                        )
                    native_artifacts.append(
                        WheelArchiveArtifact(
                            relative_path=member.as_posix(),
                            byte_length=len(content),
                            sha256=native_sha256,
                            artifact_type=suffix.lstrip("."),
                            machine_type=machine_type,
                        )
                    )
            metadata_names = [
                name
                for name in names
                if name.count("/") == 1
                and name.casefold().endswith(".dist-info/metadata")
            ]
            if len(metadata_names) != 1:
                raise OfflineBundleContractError(OfflineBundleFailureReason.WHEEL_INVALID)
            metadata_text = archive.read(metadata_names[0]).decode("utf-8")
            _validate_safe_content(metadata_names[0], metadata_text.encode("utf-8"))
    except (OSError, UnicodeDecodeError, zipfile.BadZipFile) as exc:
        raise OfflineBundleContractError(OfflineBundleFailureReason.WHEEL_INVALID) from exc
    try:
        metadata = Parser().parsestr(metadata_text)
        if (
            canonicalize_name(metadata.get("Name", "")) != parsed_distribution
            or Version(metadata.get("Version", "")) != parsed_version
        ):
            raise OfflineBundleContractError(OfflineBundleFailureReason.WHEEL_INVALID)
        requires = tuple(sorted(metadata.get_all("Requires-Dist", [])))
        content = path.read_bytes()
        ordered_tags = sorted(tags, key=str)
        return WheelInventoryEntry(
            filename=path.name,
            distribution=str(parsed_distribution),
            version=str(parsed_version),
            python_tag=".".join(sorted({tag.interpreter for tag in ordered_tags})),
            abi_tag=".".join(sorted({tag.abi for tag in ordered_tags})),
            platform_tag=".".join(sorted({tag.platform for tag in ordered_tags})),
            byte_length=len(content),
            sha256=sha256(content).hexdigest(),
            requires_dist=requires,
            native_artifacts=tuple(
                sorted(
                    native_artifacts,
                    key=lambda artifact: artifact.relative_path.casefold(),
                )
            ),
        )
    except OfflineBundleContractError:
        raise
    except (InvalidVersion, OSError, TypeError, ValueError) as exc:
        raise OfflineBundleContractError(OfflineBundleFailureReason.WHEEL_INVALID) from exc


def _marker_environment(python_version: str) -> dict[str, str]:
    environment = default_environment()
    major, minor, *_patch = python_version.split(".")
    environment.update(
        {
            "implementation_name": "cpython",
            "implementation_version": python_version,
            "os_name": "nt",
            "platform_machine": SUPPORTED_WINDOWS_ARCHITECTURE,
            "platform_python_implementation": "CPython",
            "platform_system": "Windows",
            "python_full_version": python_version,
            "python_version": f"{major}.{minor}",
            "sys_platform": "win32",
        }
    )
    return environment


def verify_wheelhouse_completeness(
    wheels: tuple[WheelInventoryEntry, ...],
    *,
    dependency_roots: tuple[str, ...],
    python_version: str,
) -> None:
    """Prove the target-specific dependency graph is closed without resolving online."""

    by_distribution: dict[str, WheelInventoryEntry] = {}
    for wheel in wheels:
        if wheel.distribution in by_distribution:
            raise OfflineBundleContractError(OfflineBundleFailureReason.DUPLICATE_DISTRIBUTION)
        by_distribution[wheel.distribution] = wheel
    environment = _marker_environment(python_version)
    pending: list[tuple[Requirement, bool]] = []
    try:
        for root in dependency_roots:
            requirement = Requirement(root)
            marker_environment = {**environment, "extra": ""}
            if requirement.marker is None or requirement.marker.evaluate(marker_environment):
                pending.append((requirement, True))
    except InvalidRequirement as exc:
        raise OfflineBundleContractError(OfflineBundleFailureReason.MANIFEST_INVALID) from exc
    reached: set[str] = set()
    processed: set[str] = set()
    active_extras: dict[str, set[str]] = {}
    while pending:
        requirement, is_root = pending.pop(0)
        name = str(canonicalize_name(requirement.name))
        wheel = by_distribution.get(name)
        if wheel is None:
            reason = (
                OfflineBundleFailureReason.ROOT_DEPENDENCY_MISSING
                if is_root
                else OfflineBundleFailureReason.MISSING_TRANSITIVE_DEPENDENCY
            )
            raise OfflineBundleContractError(reason)
        if Version(wheel.version) not in requirement.specifier:
            raise OfflineBundleContractError(
                OfflineBundleFailureReason.DEPENDENCY_VERSION_MISMATCH
            )
        reached.add(name)
        requested_extras = {extra.casefold() for extra in requirement.extras}
        known_extras = active_extras.setdefault(name, set())
        new_extras = requested_extras - known_extras
        if name in processed and not new_extras:
            continue
        known_extras.update(new_extras)
        processed.add(name)
        try:
            for value in wheel.requires_dist:
                child = Requirement(value)
                marker_extras = ("", *sorted(known_extras))
                if child.marker is None or any(
                    child.marker.evaluate({**environment, "extra": extra})
                    for extra in marker_extras
                ):
                    pending.append((child, False))
        except InvalidRequirement as exc:
            raise OfflineBundleContractError(OfflineBundleFailureReason.WHEEL_INVALID) from exc
    if reached != set(by_distribution):
        raise OfflineBundleContractError(OfflineBundleFailureReason.UNDECLARED_DEPENDENCY)


def _copy_file(source: Path, destination: Path) -> None:
    destination.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(source, destination)


def _read_bounded_bytes(path: Path, *, maximum: int) -> bytes:
    try:
        metadata = path.stat()
        if metadata.st_size < 1 or metadata.st_size > maximum:
            raise OfflineBundleContractError(OfflineBundleFailureReason.SOURCE_BINDING_MISMATCH)
        return path.read_bytes()
    except OfflineBundleContractError:
        raise
    except OSError as exc:
        raise OfflineBundleContractError(
            OfflineBundleFailureReason.SOURCE_BINDING_MISMATCH
        ) from exc


def _reviewed_source_entries(
    content: bytes,
    *,
    expected_source: OfflineBundleSourceBinding,
) -> dict[str, dict[str, object]]:
    try:
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
            raise ValueError
        entries = document["entries"]
        if not isinstance(entries, list) or not 1 <= len(entries) <= 65536:
            raise ValueError
        payload = {
            key: value for key, value in document.items() if key != "source_manifest_identity"
        }
        if (
            document["contract_version"] != expected_source.contract_version
            or document["commit_id"] != expected_source.commit_id
            or document["tree_hash"] != expected_source.tree_hash
            or document["source_manifest_identity"]
            != expected_source.source_manifest_identity
            or document["source_manifest_identity"]
            != _identity("windows-sandbox-source-manifest:v1", payload)
        ):
            raise ValueError
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
                raise ValueError
            relative_path = str(entry["relative_path"])
            candidate = _validated_archive_member_path(relative_path)
            if candidate.as_posix() != relative_path or relative_path in by_path:
                raise ValueError
            if (
                entry["git_mode"] not in {"100644", "100755"}
                or re.fullmatch(r"[0-9a-f]{40,64}", str(entry["git_object_id"])) is None
                or not isinstance(entry["byte_length"], int)
                or not 0 <= entry["byte_length"] <= 1_073_741_824
                or re.fullmatch(r"[0-9a-f]{64}", str(entry["sha256"])) is None
            ):
                raise ValueError
            ordered_paths.append(relative_path)
            by_path[relative_path] = entry
        if ordered_paths != sorted(ordered_paths):
            raise ValueError
        return by_path
    except (TypeError, ValueError, json.JSONDecodeError) as exc:
        raise OfflineBundleContractError(
            OfflineBundleFailureReason.SOURCE_BINDING_MISMATCH
        ) from exc


def _runtime_artifact_lock(content: bytes) -> dict[str, dict[str, object]]:
    try:
        document = json.loads(content)
        if not isinstance(document, dict) or set(document) != {
            "contract_version",
            "portable_git",
            "python",
        }:
            raise ValueError
        if document["contract_version"] != "windows-offline-runtime-artifact-lock-v1":
            raise ValueError
        python = document["python"]
        portable_git = document["portable_git"]
        if not isinstance(python, dict) or set(python) != {
            "architecture",
            "artifact_byte_length",
            "artifact_filename",
            "artifact_sha256",
            "implementation",
            "source_url",
            "version",
        }:
            raise ValueError
        if not isinstance(portable_git, dict) or set(portable_git) != {
            "archive_byte_length",
            "archive_filename",
            "archive_sha256",
            "required",
            "source_url",
            "version",
        }:
            raise ValueError
        if (
            python["architecture"] != SUPPORTED_WINDOWS_ARCHITECTURE
            or python["artifact_filename"]
            != f"python-{SUPPORTED_PYTHON_VERSION}-amd64.exe"
            or python["implementation"] != "CPython"
            or python["version"] != SUPPORTED_PYTHON_VERSION
            or portable_git["archive_filename"] != "MinGit-2.45.1-64-bit.zip"
            or portable_git["required"] is not True
        ):
            raise ValueError
        for entry, length_key, hash_key in (
            (python, "artifact_byte_length", "artifact_sha256"),
            (portable_git, "archive_byte_length", "archive_sha256"),
        ):
            if (
                not isinstance(entry[length_key], int)
                or not 1 <= entry[length_key] <= 1_073_741_824
                or re.fullmatch(r"[0-9a-f]{64}", str(entry[hash_key])) is None
                or not isinstance(entry["source_url"], str)
                or not 1 <= len(entry["source_url"]) <= 512
            ):
                raise ValueError
        return {"python": python, "portable_git": portable_git}
    except (TypeError, ValueError, json.JSONDecodeError) as exc:
        raise OfflineBundleContractError(
            OfflineBundleFailureReason.SOURCE_BINDING_MISMATCH
        ) from exc


def _project_dependency_roots(content: bytes) -> tuple[str, ...]:
    try:
        import tomllib

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
            raise ValueError
        return roots
    except (KeyError, TypeError, UnicodeDecodeError, ValueError) as exc:
        raise OfflineBundleContractError(
            OfflineBundleFailureReason.SOURCE_BINDING_MISMATCH
        ) from exc


def _verify_portable_git_archive(archive_path: Path, extracted_root: Path) -> None:
    try:
        archived: dict[str, tuple[str, zipfile.ZipInfo]] = {}
        expanded_total = 0
        with zipfile.ZipFile(archive_path) as archive:
            for info in archive.infolist():
                if info.is_dir():
                    continue
                relative = _validated_archive_member_path(info.filename)
                key = relative.as_posix().casefold()
                expanded_total += info.file_size
                if (
                    key in archived
                    or stat.S_ISLNK(info.external_attr >> 16)
                    or info.file_size > 268_435_456
                    or expanded_total > 1_073_741_824
                ):
                    raise OfflineBundleContractError(
                        OfflineBundleFailureReason.SOURCE_BINDING_MISMATCH
                    )
                archived[key] = (relative.as_posix(), info)
            extracted: dict[str, tuple[str, Path]] = {}
            for path in extracted_root.rglob("*"):
                if path.is_dir() and not _is_reparse_or_link(path):
                    continue
                relative = path.relative_to(extracted_root).as_posix()
                key = relative.casefold()
                if (
                    not path.is_file()
                    or _is_reparse_or_link(path)
                    or key in extracted
                ):
                    raise OfflineBundleContractError(
                        OfflineBundleFailureReason.SOURCE_BINDING_MISMATCH
                    )
                extracted[key] = (relative, path)
            if set(archived) != set(extracted):
                raise OfflineBundleContractError(
                    OfflineBundleFailureReason.SOURCE_BINDING_MISMATCH
                )
            for key in sorted(archived):
                archived_name, info = archived[key]
                extracted_name, path = extracted[key]
                content = path.read_bytes()
                if (
                    archived_name != extracted_name
                    or len(content) != info.file_size
                    or sha256(content).digest() != sha256(archive.read(info)).digest()
                ):
                    raise OfflineBundleContractError(
                        OfflineBundleFailureReason.SOURCE_BINDING_MISMATCH
                    )
    except OfflineBundleContractError:
        raise
    except (OSError, RuntimeError, zipfile.BadZipFile) as exc:
        raise OfflineBundleContractError(
            OfflineBundleFailureReason.SOURCE_BINDING_MISMATCH
        ) from exc


def _assert_content_matches_source_entry(
    path: Path,
    entry: dict[str, object],
) -> None:
    content = _read_bounded_bytes(path, maximum=1_073_741_824)
    if len(content) != entry["byte_length"] or sha256(content).hexdigest() != entry["sha256"]:
        raise OfflineBundleContractError(OfflineBundleFailureReason.SOURCE_BINDING_MISMATCH)


def _validate_reviewed_build_inputs(request: OfflineRuntimeBundleBuildRequest) -> None:
    source_entries = _reviewed_source_entries(
        _read_bounded_bytes(request.reviewed_source_manifest, maximum=8 * 1024 * 1024),
        expected_source=request.source,
    )
    input_paths = {
        _ARTIFACT_LOCK_RELATIVE_PATH: request.runtime_artifact_lock,
        _PROJECT_METADATA_RELATIVE_PATH: request.project_metadata,
        _BOOTSTRAP_RELATIVE_PATH: request.bootstrap_script,
        _VERIFIER_RELATIVE_PATH: request.verifier_script,
        _DEPENDENCY_LOCK_RELATIVE_PATH: request.expected_dependency_lock,
    }
    for bundle_path, source_path in _TRACKED_BUNDLE_BINDINGS.items():
        entry = source_entries.get(source_path)
        if entry is None:
            raise OfflineBundleContractError(
                OfflineBundleFailureReason.SOURCE_BINDING_MISMATCH
            )
        _assert_content_matches_source_entry(input_paths[bundle_path], entry)
    project_metadata = request.project_metadata.read_bytes()
    if (
        sha256(project_metadata).hexdigest() != request.project_metadata_sha256
        or _project_dependency_roots(project_metadata) != request.dependency_roots
    ):
        raise OfflineBundleContractError(OfflineBundleFailureReason.SOURCE_BINDING_MISMATCH)
    artifact_lock = _runtime_artifact_lock(request.runtime_artifact_lock.read_bytes())
    python = request.python_installer.read_bytes()
    if (
        request.python_installer.name != artifact_lock["python"]["artifact_filename"]
        or len(python) != artifact_lock["python"]["artifact_byte_length"]
        or sha256(python).hexdigest() != artifact_lock["python"]["artifact_sha256"]
    ):
        raise OfflineBundleContractError(OfflineBundleFailureReason.SOURCE_BINDING_MISMATCH)
    archive = request.portable_git_archive.read_bytes()
    if (
        request.portable_git_archive.name
        != artifact_lock["portable_git"]["archive_filename"]
        or len(archive) != artifact_lock["portable_git"]["archive_byte_length"]
        or sha256(archive).hexdigest()
        != artifact_lock["portable_git"]["archive_sha256"]
    ):
        raise OfflineBundleContractError(OfflineBundleFailureReason.SOURCE_BINDING_MISMATCH)
    _verify_portable_git_archive(request.portable_git_archive, request.portable_git_root)


def _requirements_bytes(wheels: tuple[WheelInventoryEntry, ...]) -> bytes:
    lines = [
        f"{wheel.distribution}=={wheel.version} --hash=sha256:{wheel.sha256}"
        for wheel in sorted(wheels, key=lambda item: item.distribution)
    ]
    return ("\n".join(lines) + "\n").encode("utf-8")


def _validate_supported_target(request: OfflineRuntimeBundleBuildRequest) -> None:
    if request.python_version != SUPPORTED_PYTHON_VERSION:
        raise OfflineBundleContractError(OfflineBundleFailureReason.WRONG_PYTHON_ABI)
    if request.windows_architecture != SUPPORTED_WINDOWS_ARCHITECTURE:
        raise OfflineBundleContractError(OfflineBundleFailureReason.WRONG_ARCHITECTURE)
    if not re.fullmatch(r"[0-9a-f]{64}", request.project_metadata_sha256):
        raise OfflineBundleContractError(OfflineBundleFailureReason.MANIFEST_INVALID)
    if request.python_installer.name != f"python-{SUPPORTED_PYTHON_VERSION}-amd64.exe":
        raise OfflineBundleContractError(OfflineBundleFailureReason.WRONG_ARCHITECTURE)


def _validate_build_inputs(
    request: OfflineRuntimeBundleBuildRequest,
    wheel_inputs: tuple[Path, ...],
) -> None:
    _validate_regular_input(
        request.python_installer,
        relative_path=f"python/{request.python_installer.name}",
    )
    _validate_regular_input(
        request.bootstrap_script,
        relative_path="bootstrap/install-runtime.ps1",
    )
    _validate_regular_input(
        request.verifier_script,
        relative_path="bootstrap/verify_bundle.py",
    )
    _validate_regular_input(
        request.reviewed_source_manifest,
        relative_path=_SOURCE_MANIFEST_RELATIVE_PATH,
    )
    _validate_regular_input(
        request.runtime_artifact_lock,
        relative_path=_ARTIFACT_LOCK_RELATIVE_PATH,
    )
    _validate_regular_input(
        request.project_metadata,
        relative_path=_PROJECT_METADATA_RELATIVE_PATH,
    )
    _validate_regular_input(
        request.portable_git_archive,
        relative_path=_PORTABLE_GIT_ARCHIVE_RELATIVE_PATH,
    )
    _validate_directory_root(request.portable_git_root)
    for path in request.portable_git_root.rglob("*"):
        if path.is_dir() and not _is_reparse_or_link(path):
            continue
        relative = path.relative_to(request.portable_git_root).as_posix()
        _validate_regular_input(path, relative_path=f"git/{relative}")
    _validate_directory_root(request.wheelhouse)
    wheel_input_set = set(wheel_inputs)
    try:
        wheelhouse_members = tuple(request.wheelhouse.iterdir())
    except OSError as exc:
        raise OfflineBundleContractError(OfflineBundleFailureReason.FORBIDDEN_INPUT) from exc
    for path in wheelhouse_members:
        if path not in wheel_input_set:
            if path.is_file():
                _validate_regular_input(path, relative_path=f"wheelhouse/{path.name}")
            raise OfflineBundleContractError(OfflineBundleFailureReason.FORBIDDEN_INPUT)
    for wheel in wheel_inputs:
        _validate_regular_input(wheel, relative_path=f"wheelhouse/{wheel.name}")


def build_offline_runtime_bundle(
    request: OfflineRuntimeBundleBuildRequest,
) -> BuiltOfflineRuntimeBundle:
    """Copy reviewed inputs into one deterministic, closed bundle directory."""

    _validate_supported_target(request)
    wheel_inputs = tuple(
        sorted(request.wheelhouse.glob("*.whl"), key=lambda path: path.name.casefold())
    )
    _validate_build_inputs(request, wheel_inputs)
    _validate_reviewed_build_inputs(request)
    wheels = tuple(_read_wheel(path) for path in wheel_inputs)
    verify_wheelhouse_completeness(
        wheels,
        dependency_roots=request.dependency_roots,
        python_version=request.python_version,
    )
    lock_bytes = _requirements_bytes(wheels)
    _validate_regular_input(
        request.expected_dependency_lock,
        relative_path="locks/requirements-win-cp313.txt",
    )
    if request.expected_dependency_lock.read_bytes() != lock_bytes:
        raise OfflineBundleContractError(
            OfflineBundleFailureReason.DEPENDENCY_LOCK_MISMATCH
        )
    root = request.destination
    root.mkdir(parents=True, exist_ok=False)

    python_relative = f"python/{request.python_installer.name}"
    python_target = root / Path(python_relative)
    _copy_file(request.python_installer, python_target)

    for path in wheel_inputs:
        _copy_file(path, root / "wheelhouse" / path.name)

    lock_relative = "locks/requirements-win-cp313.txt"
    lock_path = root / Path(lock_relative)
    lock_path.parent.mkdir(parents=True)
    lock_path.write_bytes(lock_bytes)

    bootstrap_relative = "bootstrap/install-runtime.ps1"
    verifier_relative = "bootstrap/verify_bundle.py"
    _copy_file(request.bootstrap_script, root / Path(bootstrap_relative))
    _copy_file(request.verifier_script, root / Path(verifier_relative))

    provenance_inputs = (
        (
            request.reviewed_source_manifest,
            _SOURCE_MANIFEST_RELATIVE_PATH,
            BundleArtifactRole.REVIEWED_SOURCE_MANIFEST,
        ),
        (
            request.runtime_artifact_lock,
            _ARTIFACT_LOCK_RELATIVE_PATH,
            BundleArtifactRole.RUNTIME_ARTIFACT_LOCK,
        ),
        (
            request.project_metadata,
            _PROJECT_METADATA_RELATIVE_PATH,
            BundleArtifactRole.PROJECT_METADATA,
        ),
        (
            request.portable_git_archive,
            _PORTABLE_GIT_ARCHIVE_RELATIVE_PATH,
            BundleArtifactRole.PORTABLE_GIT_ARCHIVE,
        ),
    )
    provenance_entries: list[BundleFileEntry] = []
    for source_path, relative_path, role in provenance_inputs:
        target = root / Path(relative_path)
        _copy_file(source_path, target)
        provenance_entries.append(_file_entry(target, relative_path, role))

    git_entries: list[BundleFileEntry] = []
    for path in sorted(
        (item for item in request.portable_git_root.rglob("*") if item.is_file()),
        key=lambda item: item.relative_to(request.portable_git_root).as_posix().casefold(),
    ):
        relative_inside_git = path.relative_to(request.portable_git_root).as_posix()
        relative = f"git/{relative_inside_git}"
        target = root / Path(relative)
        _copy_file(path, target)
        role = (
            BundleArtifactRole.PORTABLE_GIT_EXECUTABLE
            if relative_inside_git.casefold() == "cmd/git.exe"
            else BundleArtifactRole.PORTABLE_GIT_SUPPORT
        )
        git_entries.append(_file_entry(target, relative, role))
    git_relative = "git/cmd/git.exe"
    if not (root / Path(git_relative)).is_file():
        raise ValueError("portable Git must contain cmd/git.exe")

    wheel_file_entries = tuple(
        _file_entry(
            root / "wheelhouse" / wheel.filename,
            f"wheelhouse/{wheel.filename}",
            BundleArtifactRole.DEPENDENCY_WHEEL,
        )
        for wheel in wheels
    )
    fixed_entries = (
        _file_entry(
            python_target,
            python_relative,
            BundleArtifactRole.PYTHON_INSTALLER,
        ),
        _file_entry(lock_path, lock_relative, BundleArtifactRole.DEPENDENCY_LOCK),
        _file_entry(
            root / Path(bootstrap_relative),
            bootstrap_relative,
            BundleArtifactRole.BOOTSTRAP_SCRIPT,
        ),
        _file_entry(
            root / Path(verifier_relative),
            verifier_relative,
            BundleArtifactRole.VERIFIER_SCRIPT,
        ),
    )
    files = tuple(
        sorted(
            (*fixed_entries, *wheel_file_entries, *git_entries, *provenance_entries),
            key=lambda item: item.relative_path.casefold(),
        )
    )
    python_entry = next(
        entry for entry in files if entry.role is BundleArtifactRole.PYTHON_INSTALLER
    )
    lock_entry = next(entry for entry in files if entry.role is BundleArtifactRole.DEPENDENCY_LOCK)
    support_entries = tuple(
        entry
        for entry in files
        if entry.role
        in {BundleArtifactRole.BOOTSTRAP_SCRIPT, BundleArtifactRole.VERIFIER_SCRIPT}
    )
    install_policy = OfflineInstallPolicy(
        dependency_install_arguments=_DEPENDENCY_INSTALL_ARGUMENTS,
        project_install_arguments=_PROJECT_INSTALL_ARGUMENTS,
    )
    manifest_payload = {
        "contract_version": OFFLINE_RUNTIME_BUNDLE_CONTRACT_VERSION,
        "source": request.source.model_dump(mode="json"),
        "supported_windows_architecture": request.windows_architecture,
        "python_implementation": "CPython",
        "python_version": request.python_version,
        "python_tag": SUPPORTED_PYTHON_TAG,
        "abi_tag": SUPPORTED_ABI_TAG,
        "platform_tag": SUPPORTED_PLATFORM_TAG,
        "python_installer_relative_path": python_relative,
        "python_runtime_identity": _identity(
            "windows-offline-python-runtime:v1",
            python_entry.model_dump(mode="json"),
        ),
        "dependency_roots": tuple(sorted(request.dependency_roots)),
        "dependency_lock_relative_path": lock_relative,
        "dependency_lock_identity": _identity(
            "windows-offline-dependency-lock:v1",
            lock_entry.model_dump(mode="json"),
        ),
        "wheelhouse_inventory": [wheel.model_dump(mode="json") for wheel in wheels],
        "wheelhouse_inventory_identity": _identity(
            "windows-offline-wheelhouse:v1",
            [wheel.model_dump(mode="json") for wheel in wheels],
        ),
        "bootstrap_relative_path": bootstrap_relative,
        "verifier_relative_path": verifier_relative,
        "bootstrap_support_identity": _identity(
            "windows-offline-bootstrap-support:v1",
            [entry.model_dump(mode="json") for entry in support_entries],
        ),
        "portable_git_required": True,
        "portable_git_relative_path": git_relative,
        "portable_git_identity": _identity(
            "windows-offline-portable-git:v1",
            [entry.model_dump(mode="json") for entry in git_entries],
        ),
        "reviewed_source_manifest_relative_path": _SOURCE_MANIFEST_RELATIVE_PATH,
        "runtime_artifact_lock_relative_path": _ARTIFACT_LOCK_RELATIVE_PATH,
        "project_metadata_relative_path": _PROJECT_METADATA_RELATIVE_PATH,
        "portable_git_archive_relative_path": _PORTABLE_GIT_ARCHIVE_RELATIVE_PATH,
        "reviewed_input_identity": _identity(
            "windows-offline-reviewed-inputs:v1",
            [entry.model_dump(mode="json") for entry in provenance_entries],
        ),
        "project_metadata_sha256": request.project_metadata_sha256,
        "iso01_runtime_bundle_input_contract_version": (
            "windows-sandbox-runtime-bundle-input-v1"
        ),
        "files": [entry.model_dump(mode="json") for entry in files],
        "install_policy": install_policy.model_dump(mode="json"),
    }
    manifest = OfflineRuntimeBundleManifest(
        **manifest_payload,
        bundle_identity=_identity("windows-offline-runtime-bundle:v1", manifest_payload),
    )
    manifest_path = root / BUNDLE_MANIFEST_FILENAME
    manifest_bytes = manifest.canonical_json_bytes() + b"\n"
    manifest_path.write_bytes(manifest_bytes)
    verify_offline_runtime_bundle(root, expected_source=request.source)
    return BuiltOfflineRuntimeBundle(
        root=root,
        manifest_path=manifest_path,
        manifest=manifest,
        manifest_sha256=sha256(manifest_bytes).hexdigest(),
    )


def _is_reparse_or_link(path: Path) -> bool:
    metadata = path.lstat()
    return stat.S_ISLNK(metadata.st_mode) or bool(
        getattr(metadata, "st_file_attributes", 0) & _WINDOWS_REPARSE_POINT_ATTRIBUTE
    )


def _validate_reviewed_bundle_provenance(
    root: Path,
    manifest: OfflineRuntimeBundleManifest,
) -> None:
    source_entries = _reviewed_source_entries(
        _read_bounded_bytes(
            root / Path(manifest.reviewed_source_manifest_relative_path),
            maximum=8 * 1024 * 1024,
        ),
        expected_source=manifest.source,
    )
    for bundle_path, source_path in _TRACKED_BUNDLE_BINDINGS.items():
        source_entry = source_entries.get(source_path)
        if source_entry is None:
            raise OfflineBundleContractError(
                OfflineBundleFailureReason.SOURCE_BINDING_MISMATCH
            )
        _assert_content_matches_source_entry(root / Path(bundle_path), source_entry)
    project_metadata = (root / Path(manifest.project_metadata_relative_path)).read_bytes()
    if (
        sha256(project_metadata).hexdigest() != manifest.project_metadata_sha256
        or _project_dependency_roots(project_metadata) != manifest.dependency_roots
    ):
        raise OfflineBundleContractError(OfflineBundleFailureReason.SOURCE_BINDING_MISMATCH)
    artifact_lock = _runtime_artifact_lock(
        (root / Path(manifest.runtime_artifact_lock_relative_path)).read_bytes()
    )
    python_path = root / Path(manifest.python_installer_relative_path)
    python = python_path.read_bytes()
    if (
        python_path.name != artifact_lock["python"]["artifact_filename"]
        or len(python) != artifact_lock["python"]["artifact_byte_length"]
        or sha256(python).hexdigest() != artifact_lock["python"]["artifact_sha256"]
    ):
        raise OfflineBundleContractError(OfflineBundleFailureReason.SOURCE_BINDING_MISMATCH)
    archive_path = root / Path(manifest.portable_git_archive_relative_path)
    archive = archive_path.read_bytes()
    if (
        archive_path.name != artifact_lock["portable_git"]["archive_filename"]
        or len(archive) != artifact_lock["portable_git"]["archive_byte_length"]
        or sha256(archive).hexdigest()
        != artifact_lock["portable_git"]["archive_sha256"]
    ):
        raise OfflineBundleContractError(OfflineBundleFailureReason.SOURCE_BINDING_MISMATCH)
    _verify_portable_git_archive(archive_path, root / "git")


def verify_offline_runtime_bundle(
    root: Path,
    *,
    expected_source: OfflineBundleSourceBinding,
) -> OfflineRuntimeBundleManifest:
    """Verify the entire bundle and dependency closure without executing an installer."""

    try:
        resolved = root.resolve(strict=True)
        manifest_path = resolved / BUNDLE_MANIFEST_FILENAME
        manifest_bytes = manifest_path.read_bytes()
        if len(manifest_bytes) > 8 * 1024 * 1024 or _is_reparse_or_link(manifest_path):
            raise OfflineBundleContractError(OfflineBundleFailureReason.MANIFEST_INVALID)
        manifest = OfflineRuntimeBundleManifest.model_validate_json(manifest_bytes)
    except OfflineBundleContractError:
        raise
    except (OSError, ValueError, TypeError) as exc:
        reason = (
            OfflineBundleFailureReason.MANIFEST_IDENTITY_MISMATCH
            if "identity mismatch" in str(exc)
            else OfflineBundleFailureReason.MANIFEST_INVALID
        )
        raise OfflineBundleContractError(reason) from exc
    if manifest.source != expected_source:
        raise OfflineBundleContractError(OfflineBundleFailureReason.SOURCE_BINDING_MISMATCH)
    expected_paths = {entry.relative_path for entry in manifest.files}
    actual_paths: set[str] = set()
    for path in resolved.rglob("*"):
        if _is_reparse_or_link(path):
            raise OfflineBundleContractError(OfflineBundleFailureReason.REPARSE_POINT)
        if path.is_dir():
            continue
        if not path.is_file():
            raise OfflineBundleContractError(OfflineBundleFailureReason.FORBIDDEN_INPUT)
        relative = path.relative_to(resolved).as_posix()
        if relative != BUNDLE_MANIFEST_FILENAME:
            actual_paths.add(relative)
    missing = expected_paths - actual_paths
    if missing:
        raise OfflineBundleContractError(OfflineBundleFailureReason.FILE_MISSING)
    if actual_paths - expected_paths:
        raise OfflineBundleContractError(OfflineBundleFailureReason.FILE_EXTRA)
    by_path = {entry.relative_path: entry for entry in manifest.files}
    for relative_path in sorted(actual_paths):
        entry = by_path[relative_path]
        path = resolved / Path(relative_path)
        try:
            metadata = path.stat()
            if metadata.st_size != entry.byte_length:
                raise OfflineBundleContractError(
                    OfflineBundleFailureReason.FILE_LENGTH_MISMATCH
                )
            if sha256(path.read_bytes()).hexdigest() != entry.sha256:
                raise OfflineBundleContractError(OfflineBundleFailureReason.FILE_HASH_MISMATCH)
        except OfflineBundleContractError:
            raise
        except OSError as exc:
            raise OfflineBundleContractError(OfflineBundleFailureReason.FILE_MISSING) from exc
    _validate_reviewed_bundle_provenance(resolved, manifest)
    wheel_entries = tuple(
        _read_wheel(resolved / "wheelhouse" / wheel.filename)
        for wheel in manifest.wheelhouse_inventory
    )
    if wheel_entries != manifest.wheelhouse_inventory:
        raise OfflineBundleContractError(OfflineBundleFailureReason.FILE_HASH_MISMATCH)
    verify_wheelhouse_completeness(
        wheel_entries,
        dependency_roots=manifest.dependency_roots,
        python_version=manifest.python_version,
    )
    lock_path = resolved / Path(manifest.dependency_lock_relative_path)
    if lock_path.read_bytes() != _requirements_bytes(wheel_entries):
        raise OfflineBundleContractError(OfflineBundleFailureReason.DEPENDENCY_LOCK_MISMATCH)
    return manifest


def _guest_bundle_path(relative_path: str) -> str:
    return str(Path(r"C:\QCF17\Input\Runtime") / Path(relative_path)).replace("/", "\\")


def build_offline_install_plan(
    manifest: OfflineRuntimeBundleManifest,
) -> OfflineProvisioningPlan:
    """Render the exact bounded guest-local subprocess plan."""

    python_install = r"C:\QCF17\Python"
    venv = r"C:\QCF17\Venv"
    installed_python = rf"{python_install}\python.exe"
    venv_python = rf"{venv}\Scripts\python.exe"
    commands = (
        OfflineProvisioningCommand(
            role=ProvisioningCommandRole.INSTALL_PYTHON,
            argv=(
                _guest_bundle_path(manifest.python_installer_relative_path),
                "/quiet",
                "InstallAllUsers=0",
                f"TargetDir={python_install}",
                "Include_launcher=0",
                "Include_pip=1",
                "Include_test=0",
                "PrependPath=0",
                "Shortcuts=0",
            ),
        ),
        OfflineProvisioningCommand(
            role=ProvisioningCommandRole.CREATE_VIRTUAL_ENVIRONMENT,
            argv=(installed_python, "-I", "-m", "venv", "--clear", venv),
        ),
        OfflineProvisioningCommand(
            role=ProvisioningCommandRole.VERIFY_WHEELHOUSE,
            argv=(
                installed_python,
                "-I",
                _guest_bundle_path(manifest.verifier_relative_path),
                "--bundle-root",
                r"C:\QCF17\Input\Runtime",
                "--expected-bundle-identity",
                manifest.bundle_identity,
                "--expected-source-input-identity",
                manifest.source.source_input_identity,
            ),
        ),
        OfflineProvisioningCommand(
            role=ProvisioningCommandRole.INSTALL_DEPENDENCIES,
            argv=(venv_python, *manifest.install_policy.dependency_install_arguments),
        ),
        OfflineProvisioningCommand(
            role=ProvisioningCommandRole.INSTALL_PROJECT,
            argv=(venv_python, *manifest.install_policy.project_install_arguments),
        ),
    )
    environment = (
        ("PIP_CONFIG_FILE", "NUL"),
        ("PIP_DISABLE_PIP_VERSION_CHECK", "1"),
        ("PIP_NO_INDEX", "1"),
        ("PYTHONNOUSERSITE", "1"),
    )
    payload = {
        "contract_version": "windows-offline-provisioning-plan-v1",
        "bundle_identity": manifest.bundle_identity,
        "verification_required_before_execution": True,
        "network_permitted": False,
        "host_python_permitted": False,
        "environment": [list(item) for item in environment],
        "commands": [command.model_dump(mode="json") for command in commands],
    }
    return OfflineProvisioningPlan(
        **payload,
        plan_identity=_identity("windows-offline-provisioning-plan:v1", payload),
    )


ProvisioningExecutor = Callable[[tuple[str, ...], tuple[tuple[str, str], ...]], int]


def execute_offline_provisioning(
    bundle_root: Path,
    *,
    expected_source: OfflineBundleSourceBinding,
    executor: ProvisioningExecutor,
) -> OfflineProvisioningEvidence:
    """Verify first, then execute a safe plan without retaining subprocess output."""

    manifest = verify_offline_runtime_bundle(bundle_root, expected_source=expected_source)
    plan = build_offline_install_plan(manifest)
    completed: list[ProvisioningCommandRole] = []
    for command in plan.commands:
        returncode = executor(command.argv, plan.environment)
        if returncode != 0:
            raise OfflineBundleContractError(OfflineBundleFailureReason.MANIFEST_INVALID)
        completed.append(command.role)
    payload = {
        "contract_version": "windows-offline-provisioning-evidence-v1",
        "bundle_identity": manifest.bundle_identity,
        "plan_identity": plan.plan_identity,
        "status": "provisioned",
        "completed_roles": [role.value for role in completed],
        "network_used": False,
        "host_python_used": False,
    }
    return OfflineProvisioningEvidence(
        **payload,
        evidence_identity=_identity("windows-offline-provisioning-evidence:v1", payload),
    )

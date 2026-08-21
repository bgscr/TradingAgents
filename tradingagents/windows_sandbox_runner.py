"""Versioned host contract for disposable Windows Sandbox acceptance runs.

ISO-01 owns construction and deterministic validation only.  Boundary
qualification, the offline runtime bundle, and Ticket 06 execution are later
tickets and are deliberately absent from this module.
"""

from __future__ import annotations

import csv
import io
import json
import os
import re
import shutil
import stat
import subprocess
import sys
import tarfile
import time
from collections.abc import Iterable
from dataclasses import dataclass, replace
from enum import Enum
from hashlib import sha256
from pathlib import Path, PureWindowsPath
from typing import Literal, Protocol
from xml.etree import ElementTree
from xml.sax.saxutils import escape as xml_escape

from packaging.version import InvalidVersion, Version
from pydantic import BaseModel, ConfigDict, Field, model_validator

from tradingagents.offline_runtime_bundle import (
    BUNDLE_MANIFEST_FILENAME,
    OfflineBundleSourceBinding,
    OfflineRuntimeBundleManifest,
    verify_offline_runtime_bundle,
)

PROTECTED_ROOT_MANIFEST_V1_VERSION = "windows-sandbox-protected-roots-v1"
PROTECTED_ROOT_MANIFEST_V2_VERSION = "windows-sandbox-protected-roots-v2"
PROTECTED_ROOT_MANIFEST_VERSION = "windows-sandbox-protected-roots-v3"
SOURCE_INPUT_CONTRACT_VERSION = "windows-sandbox-reviewed-source-v1"
GUEST_LAYOUT_CONTRACT_VERSION = "windows-sandbox-guest-layout-v1"
ENVIRONMENT_POLICY_CONTRACT_VERSION = "windows-sandbox-environment-policy-v1"
MAPPING_MANIFEST_CONTRACT_VERSION = "windows-sandbox-mapping-manifest-v1"
WSB_CONFIGURATION_CONTRACT_VERSION = "windows-sandbox-config-v1"
RUNTIME_BUNDLE_INPUT_CONTRACT_VERSION = "windows-sandbox-runtime-bundle-input-v1"
RESULT_SCHEMA_CONTRACT_VERSION = "windows-sandbox-result-v1"
COMPLETION_MARKER_CONTRACT_VERSION = "windows-sandbox-completion-marker-v1"
RUNNER_CONTRACT_VERSION = "iso01-windows-sandbox-runner-v1"
_CLOSED_MODEL_CONFIG = ConfigDict(frozen=True, extra="forbid")
_WINDOWS_REPARSE_POINT_ATTRIBUTE = 0x400
_RUN_LIVE_PATTERN = re.compile(rb"\bRUN_LIVE_[A-Z0-9_]+\b")
_HASH_LOCKED_REQUIREMENT_PATTERN = re.compile(
    r"^[a-z0-9][a-z0-9._-]*==(?P<version>[A-Za-z0-9][A-Za-z0-9.!+_-]*) "
    r"--hash=sha256:[0-9a-f]{64}$"
)
_CREDENTIAL_NAME_PATTERN = r"[A-Z0-9_]*(?:API_?KEY|TOKEN|PASSWORD|SECRET|CREDENTIALS?)"
_CREDENTIAL_KEY_PATTERN = re.compile(
    rf"^(?:{_CREDENTIAL_NAME_PATTERN})$",
    flags=re.IGNORECASE,
)
_CREDENTIAL_ASSIGNMENT_PATTERN = re.compile(
    rf"(?im)^[ \t]*(?:[{{,][ \t]*)?(?:export[ \t]+|\$env:)?"
    rf"(?:['\"]{_CREDENTIAL_NAME_PATTERN}['\"]|{_CREDENTIAL_NAME_PATTERN})"
    r"[ \t]*[:=][ \t]*(?:\"([^\"\r\n]*)\"|'([^'\r\n]*)'|([^\s#,;]+))"
)
_CREDENTIAL_URL_PATTERN = re.compile(r"(?i)https?://[^\s/:@]+:[^\s/@]+@[^\s/'\"?#]+")
_KNOWN_SECRET_PATTERN = re.compile(
    r"(?:-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----|"
    r"\bAKIA[0-9A-Z]{16}\b|\bgh[oprsu]_[A-Za-z0-9]{20,}\b|"
    r"\bsk-[A-Za-z0-9_-]{20,}\b)"
)
_PLACEHOLDER_LITERAL_PATTERN = re.compile(
    r"(?:example|dummy|fake|placeholder|redacted|test)"
    r"(?:[-_](?:api|canary|credential|fixture|for|invalid|key|only|password|"
    r"secret|tests?|token|value))*"
)
_TEST_FIXTURE_LITERAL_PATTERN = re.compile(
    r"(?i)(?:(?:canary|dummy|example|fake|fixture|placeholder|redacted|sentinel|test)"
    r"(?:[-_ ][a-z0-9]+){0,12}|(?:[a-z0-9]+[-_ ]){0,4}"
    r"(?:must[-_ ]not|not[-_ ]a[-_ ]real|never[-_ ]use|do[-_ ]not)"
    r"(?:[-_ ][a-z0-9]+){0,12})"
)
_TEST_FIXTURE_LITERAL_SHA256 = frozenset(
    {
        "344171e7b024a65cd9f31c0d5f834f9e69694516471c12c444612016d9832c33",
        "48b49b0c01981c331b047f0dfb1f729ba428931c5c16a228d3a0d1c22db13511",
        "4fe57cca1dd215d0a209841e7d16feaa17cf02bc919413c6965b8827a6c0ef9a",
        "53576542a981038931b99cebc755aea96680664a1c01bb96687ba60d78bc4e07",
        "54ad5adee189019bfe3b5d2ca60ac4e972a99fb1b84a7dd7192e83be1b42d766",
        "7de9e99b34ff2dd7499f05988cccf1260a96c28c1a6e9952c3f87084f16d1620",
        "8ad82619476a7a41332261e43304323ac903060ec4a38ac7ccbdab804b4396ab",
        "9b1e60aa5e571d7229d365aed6797df7e6568eb19d630c5ab93c5ca75e93ed0b",
        "c2c7aaf98100c2eb067e112d9c3edb0fde80bd32f34e089634bdc33644d1fe66",
        "cebdf378f2d2bd60d3e3693349e8f20f329daf11a276f849996f0a6377e2155b",
        "d0b288e5c229126b090b8748c715be16dc1ffbe250cdb995044174ac6edf2576",
        "d6fcfdf4e3b2b69b1a1b4cd9ee017fff9307d23e61d3493766c79619c0cd6e85",
        "df99d4c76512090d85f4349b68db987a4e6f50aa6fcad691e3e07e82c9f397e1",
        "e636df604701e087395ea14d6ec9bfd731b6619ed7542106054dce9fe2e29edf",
        "e8fd8f58d62af6d4c8722fe6704b79d2d3d74922386e2e4462c479cf8991e6f7",
        "ea77193cc4e6f18656f3130e296203880c4b9b3772afc855211b82fdd46e9185",
        "f49576a5514efd3619e975a87c8a97cc766fdfac00f42d33dc34515a32c3fcba",
        "f53b7859e89fdec5db5a1a16bb2ae2a7042bf677d274fd25e8e3242612f7843e",
    }
)
_TEST_FIXTURE_PEM_SHA256 = frozenset(
    {"bad1e9aef255bb432a81430de57971b3d3e92fac7344063b8331213a13844343"}
)
_MACHINE_IDENTITY_PATTERN_TEXT = (
    r"^[a-z0-9][a-z0-9._-]*(?::[a-z0-9][a-z0-9._-]*)*"
    r":sha256:[0-9a-f]{64}$"
)
_MACHINE_IDENTITY_PATTERN = re.compile(_MACHINE_IDENTITY_PATTERN_TEXT)


class SandboxFailureReason(str, Enum):
    """Closed, secret-free host preflight failure vocabulary."""

    PROTECTED_ROOT_MISSING = "protected_root_missing"
    PROTECTED_ROOT_KIND_MISMATCH = "protected_root_kind_mismatch"
    PROTECTED_ROOT_REPARSE_POINT = "protected_root_reparse_point"
    PROTECTED_ROOT_MANIFEST_INCOMPLETE = "protected_root_manifest_incomplete"
    PROTECTED_ROOT_MANIFEST_DUPLICATE = "protected_root_manifest_duplicate"
    PROTECTED_ROOT_MANIFEST_OVERLAP = "protected_root_manifest_overlap"
    PROTECTED_ROOT_EVIDENCE_INVALID = "protected_root_evidence_invalid"
    SOURCE_REPOSITORY_INVALID = "source_repository_invalid"
    SOURCE_NOT_CLEAN = "source_not_clean"
    SOURCE_INDEX_MISMATCH = "source_index_mismatch"
    SOURCE_UNSUPPORTED_ENTRY = "source_unsupported_entry"
    SOURCE_IDENTITY_MISMATCH = "source_identity_mismatch"
    FORBIDDEN_INPUT = "forbidden_input"
    CREDENTIAL_INPUT = "credential_input"
    GUEST_LAYOUT_INVALID = "guest_layout_invalid"
    ENVIRONMENT_POLICY_INVALID = "environment_policy_invalid"
    MAPPING_CONTRACT_INVALID = "mapping_contract_invalid"
    WRITABLE_INPUT_MAPPING = "writable_input_mapping"
    ADDITIONAL_WRITABLE_MAPPING = "additional_writable_mapping"
    RESULTS_EXPORT_NOT_FRESH = "results_export_not_fresh"
    PROTECTED_HOST_MAPPING = "protected_host_mapping"
    FORBIDDEN_HOST_MAPPING = "forbidden_host_mapping"
    OVERLAPPING_MAPPING = "overlapping_mapping"
    NETWORKING_ENABLED = "networking_enabled"
    UNSAFE_RESULT_EXPORT = "unsafe_result_export"
    COMPLETION_MARKER_INVALID = "completion_marker_invalid"
    RESULT_IDENTITY_MISMATCH = "result_identity_mismatch"
    PLATFORM_UNAVAILABLE = "platform_unavailable"
    PLATFORM_CONTRACT_INVALID = "platform_contract_invalid"
    RUNTIME_BUNDLE_NOT_READY = "runtime_bundle_not_ready"
    SANDBOX_LAUNCH_FAILED = "sandbox_launch_failed"


class SandboxPlatformState(str, Enum):
    AVAILABLE = "available"
    NEEDS_INFO = "needs-info"
    NOT_QUALIFIED = "not-qualified"


class SandboxFeatureState(str, Enum):
    ENABLED = "enabled"
    DISABLED = "disabled"
    UNKNOWN = "unknown"


class SandboxQualificationOutcome(str, Enum):
    CONSTRUCTION_VALIDATED = "construction-validated"
    NEEDS_INFO = "needs-info"
    NOT_QUALIFIED = "not-qualified"


class SandboxContractError(ValueError):
    """Typed bounded refusal at the Windows Sandbox host boundary."""

    def __init__(
        self,
        reason: SandboxFailureReason,
        *,
        qualification_outcome: SandboxQualificationOutcome = (
            SandboxQualificationOutcome.NOT_QUALIFIED
        ),
    ) -> None:
        self.reason = reason
        self.qualification_outcome = qualification_outcome
        super().__init__(reason.value)


class ProtectedObjectKind(str, Enum):
    FILE = "file"
    DIRECTORY = "directory"


class ProtectedRootRole(str, Enum):
    """Closed ISO-01 protected set, including all Ticket 06 verifier inputs."""

    APPLICATION_PROFILE = "application_profile"
    PROVIDER_LEDGER = "provider_ledger"
    PROVIDER_LEDGER_WAL = "provider_ledger_wal"
    PROVIDER_LEDGER_SHM = "provider_ledger_shm"
    PROVIDER_LEDGER_PAYLOADS = "provider_ledger_payloads"
    PAYLOAD_MUTATION_LEDGER = "payload_mutation_ledger"
    PROVIDER_STATE = "provider_state"
    CALENDAR_DATABASE = "calendar_database"
    CALENDAR_DATABASE_WAL = "calendar_database_wal"
    CALENDAR_DATABASE_SHM = "calendar_database_shm"
    CALENDAR_STATE = "calendar_state"
    CALENDAR_PAYLOADS = "calendar_payloads"
    CHECKPOINTS = "checkpoints"
    REPORTS_LOGS = "reports_logs"
    TICKET07_EVIDENCE = "ticket07_evidence"
    PRIOR_INVENTORIES = "prior_inventories"
    FAKE_YAHOO_INCIDENTS = "fake_yahoo_incidents"
    ISOLATED_PYTHON_WRITE_PROBE = "isolated_python_write_probe"
    PRIOR_SANDBOX_QUALIFICATIONS = "prior_sandbox_qualifications"
    REPLACEMENT_SANDBOX_QUALIFICATION = "replacement_sandbox_qualification"
    QUALIFIED_REPLACEMENT_SANDBOX_QUALIFICATION = (
        "qualified_replacement_sandbox_qualification"
    )
    FAILED_ISO04_TICKET06_ACCEPTANCE = "failed_iso04_ticket06_acceptance"
    REVIEW_INVALIDATED_SANDBOX_QUALIFICATION = (
        "review_invalidated_sandbox_qualification"
    )


_OPTIONAL_SQLITE_COMPANION_ROLES = frozenset(
    {
        ProtectedRootRole.PROVIDER_LEDGER_WAL,
        ProtectedRootRole.PROVIDER_LEDGER_SHM,
        ProtectedRootRole.CALENDAR_DATABASE_WAL,
        ProtectedRootRole.CALENDAR_DATABASE_SHM,
    }
)


@dataclass(frozen=True, slots=True)
class ProtectedRootBinding:
    """Host-only path binding; absolute paths never enter safe projections."""

    role: ProtectedRootRole
    path: Path
    object_kind: ProtectedObjectKind


@dataclass(frozen=True, slots=True)
class ProtectedPathMetadata:
    canonical_path: Path
    object_kind: ProtectedObjectKind
    is_reparse_point: bool

    @classmethod
    def for_concrete(
        cls,
        path: Path,
        *,
        expected_kind: ProtectedObjectKind,
        concrete_object_required: bool = True,
    ) -> ProtectedPathMetadata:
        candidate = path.expanduser().absolute()
        try:
            metadata = candidate.lstat()
        except FileNotFoundError as exc:
            if concrete_object_required:
                raise SandboxContractError(
                    SandboxFailureReason.PROTECTED_ROOT_MISSING
                ) from exc
            metadata = None
        except OSError as exc:
            raise SandboxContractError(SandboxFailureReason.PROTECTED_ROOT_MISSING) from exc
        actual_kind = (
            ProtectedObjectKind.DIRECTORY
            if metadata is not None and stat.S_ISDIR(metadata.st_mode)
            else ProtectedObjectKind.FILE
            if metadata is not None and stat.S_ISREG(metadata.st_mode)
            else expected_kind
            if metadata is None
            else None
        )
        is_reparse = metadata is not None and (
            stat.S_ISLNK(metadata.st_mode)
            or bool(
                getattr(metadata, "st_file_attributes", 0)
                & _WINDOWS_REPARSE_POINT_ATTRIBUTE
            )
        )
        ancestor = candidate.parent
        while ancestor != ancestor.parent:
            try:
                ancestor_metadata = ancestor.lstat()
            except OSError as exc:
                raise SandboxContractError(SandboxFailureReason.PROTECTED_ROOT_MISSING) from exc
            is_reparse = (
                is_reparse
                or stat.S_ISLNK(ancestor_metadata.st_mode)
                or bool(
                    getattr(ancestor_metadata, "st_file_attributes", 0)
                    & _WINDOWS_REPARSE_POINT_ATTRIBUTE
                )
            )
            ancestor = ancestor.parent
        if actual_kind is not expected_kind:
            raise SandboxContractError(SandboxFailureReason.PROTECTED_ROOT_KIND_MISMATCH)
        try:
            canonical = candidate.resolve(strict=concrete_object_required)
        except OSError as exc:
            raise SandboxContractError(SandboxFailureReason.PROTECTED_ROOT_MISSING) from exc
        return cls(
            canonical_path=canonical,
            object_kind=expected_kind,
            is_reparse_point=is_reparse,
        )

class ProtectedPathInspector(Protocol):
    def inspect(
        self,
        path: Path,
        *,
        expected_kind: ProtectedObjectKind,
    ) -> ProtectedPathMetadata: ...


class _SystemProtectedPathInspector:
    def inspect(
        self,
        path: Path,
        *,
        expected_kind: ProtectedObjectKind,
    ) -> ProtectedPathMetadata:
        return ProtectedPathMetadata.for_concrete(path, expected_kind=expected_kind)


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


def _safe_path_identity(path: Path) -> str:
    normalized = os.path.normcase(str(path)).replace("/", "\\").casefold()
    return _identity("host-path", normalized)


class ProtectedRootEntry(BaseModel):
    model_config = _CLOSED_MODEL_CONFIG

    role: ProtectedRootRole
    object_kind: ProtectedObjectKind
    path_identity: str = Field(pattern=r"^host-path:sha256:[0-9a-f]{64}$")
    concrete_object_required: bool = True
    reparse_points_permitted: bool = False


class ProtectedRootManifest(BaseModel):
    model_config = _CLOSED_MODEL_CONFIG

    contract_version: Literal[
        "windows-sandbox-protected-roots-v1",
        "windows-sandbox-protected-roots-v2",
        "windows-sandbox-protected-roots-v3",
    ] = PROTECTED_ROOT_MANIFEST_VERSION
    entries: tuple[ProtectedRootEntry, ...]
    manifest_identity: str = Field(
        pattern=r"^windows-sandbox-protected-roots:v[123]:sha256:[0-9a-f]{64}$"
    )

    @model_validator(mode="after")
    def _validate_identity(self) -> ProtectedRootManifest:
        v1_forward_only_roles = {
            ProtectedRootRole.QUALIFIED_REPLACEMENT_SANDBOX_QUALIFICATION,
            ProtectedRootRole.FAILED_ISO04_TICKET06_ACCEPTANCE,
            ProtectedRootRole.REVIEW_INVALIDATED_SANDBOX_QUALIFICATION,
        }
        if self.contract_version == PROTECTED_ROOT_MANIFEST_V1_VERSION:
            expected_roles = tuple(
                role
                for role in ProtectedRootRole
                if role not in v1_forward_only_roles
            )
            identity_version = "v1"
        elif self.contract_version == PROTECTED_ROOT_MANIFEST_V2_VERSION:
            expected_roles = tuple(
                role
                for role in ProtectedRootRole
                if role
                is not ProtectedRootRole.REVIEW_INVALIDATED_SANDBOX_QUALIFICATION
            )
            identity_version = "v2"
        else:
            expected_roles = tuple(ProtectedRootRole)
            identity_version = "v3"
        if tuple(entry.role for entry in self.entries) != expected_roles:
            raise ValueError("protected-root roles must use canonical closed ordering")
        payload = {
            "contract_version": self.contract_version,
            "entries": [entry.model_dump(mode="json") for entry in self.entries],
        }
        if self.manifest_identity != _identity(
            f"windows-sandbox-protected-roots:{identity_version}",
            payload,
        ):
            raise ValueError("protected-root manifest identity mismatch")
        return self

    def safe_projection(self) -> dict[str, object]:
        return self.model_dump(mode="json")

    def canonical_json(self) -> str:
        return _canonical_json_bytes(self.safe_projection()).decode("utf-8")


@dataclass(frozen=True, slots=True)
class ResolvedProtectedRootManifest:
    """Safe manifest plus host-only bindings used for overlap validation."""

    contract: ProtectedRootManifest
    bindings: tuple[ProtectedRootBinding, ...]

    @property
    def contract_version(self) -> str:
        return self.contract.contract_version

    @property
    def entries(self) -> tuple[ProtectedRootEntry, ...]:
        return self.contract.entries

    @property
    def manifest_identity(self) -> str:
        return self.contract.manifest_identity

    def safe_projection(self) -> dict[str, object]:
        return self.contract.safe_projection()

    def canonical_json(self) -> str:
        return self.contract.canonical_json()


def build_protected_root_manifest(
    bindings: tuple[ProtectedRootBinding, ...],
    *,
    path_inspector: ProtectedPathInspector | None = None,
) -> ResolvedProtectedRootManifest:
    """Resolve the complete protected set without silently dropping a path."""

    inspector = path_inspector or _SystemProtectedPathInspector()
    by_role: dict[ProtectedRootRole, ProtectedRootBinding] = {}
    for binding in bindings:
        if binding.role in by_role:
            raise SandboxContractError(SandboxFailureReason.PROTECTED_ROOT_MANIFEST_DUPLICATE)
        by_role[binding.role] = binding
    if set(by_role) != set(ProtectedRootRole):
        raise SandboxContractError(SandboxFailureReason.PROTECTED_ROOT_MANIFEST_INCOMPLETE)

    resolved_bindings: list[ProtectedRootBinding] = []
    entries: list[ProtectedRootEntry] = []
    for role in ProtectedRootRole:
        binding = by_role[role]
        concrete_object_required = role not in _OPTIONAL_SQLITE_COMPANION_ROLES
        try:
            metadata = inspector.inspect(binding.path, expected_kind=binding.object_kind)
        except SandboxContractError as exc:
            if (
                concrete_object_required
                or exc.reason is not SandboxFailureReason.PROTECTED_ROOT_MISSING
            ):
                raise
            metadata = ProtectedPathMetadata.for_concrete(
                binding.path,
                expected_kind=binding.object_kind,
                concrete_object_required=False,
            )
        if metadata.is_reparse_point:
            raise SandboxContractError(SandboxFailureReason.PROTECTED_ROOT_REPARSE_POINT)
        resolved_binding = ProtectedRootBinding(
            role=role,
            path=metadata.canonical_path,
            object_kind=metadata.object_kind,
        )
        resolved_bindings.append(resolved_binding)
        entries.append(
            ProtectedRootEntry(
                role=role,
                object_kind=metadata.object_kind,
                path_identity=_safe_path_identity(metadata.canonical_path),
                concrete_object_required=concrete_object_required,
            )
        )
    payload = {
        "contract_version": PROTECTED_ROOT_MANIFEST_VERSION,
        "entries": [entry.model_dump(mode="json") for entry in entries],
    }
    contract = ProtectedRootManifest(
        entries=tuple(entries),
        manifest_identity=_identity("windows-sandbox-protected-roots:v3", payload),
    )
    return ResolvedProtectedRootManifest(
        contract=contract,
        bindings=tuple(resolved_bindings),
    )


def default_protected_root_bindings(
    *,
    user_profile: Path,
    repository_root: Path,
    historical_qualification_runs_root: Path | None = None,
    replacement_qualification_run_root: Path | None = None,
    qualified_replacement_qualification_root: Path | None = None,
    failed_iso04_ticket06_acceptance_root: Path | None = None,
    review_invalidated_qualification_root: Path | None = None,
) -> tuple[ProtectedRootBinding, ...]:
    """Return the reviewed ISO-01 roles without testing whether they exist."""

    application_root = user_profile / ".tradingagents"
    history_root = application_root / "market_history"
    calendar_database = history_root / "market_history.sqlite3"
    provider_ledger = history_root / "market_history.sqlite3.provider-requests.sqlite3"
    provider_payloads = history_root / "market_history.sqlite3.provider-requests.sqlite3.payloads"
    payload_mutation_ledger = history_root / "market_history.sqlite3.payload-mutation.sqlite3"
    payload_root = history_root / "payloads"
    evidence_root = repository_root / ".scratch" / "mainland-qualified-routing-composition-factory"
    prior_sandbox_qualifications = historical_qualification_runs_root or (
        evidence_root / "ticket06-post-cal-evidence"
    )
    replacement_sandbox_qualification = (
        replacement_qualification_run_root or prior_sandbox_qualifications
    )
    qualified_replacement_sandbox_qualification = (
        qualified_replacement_qualification_root
        or replacement_sandbox_qualification
    )
    failed_iso04_ticket06_acceptance = (
        failed_iso04_ticket06_acceptance_root
        or qualified_replacement_sandbox_qualification
    )
    review_invalidated_sandbox_qualification = (
        review_invalidated_qualification_root
        or failed_iso04_ticket06_acceptance
    )
    return (
        ProtectedRootBinding(
            ProtectedRootRole.APPLICATION_PROFILE,
            application_root,
            ProtectedObjectKind.DIRECTORY,
        ),
        ProtectedRootBinding(
            ProtectedRootRole.PROVIDER_LEDGER,
            provider_ledger,
            ProtectedObjectKind.FILE,
        ),
        ProtectedRootBinding(
            ProtectedRootRole.PROVIDER_LEDGER_WAL,
            Path(f"{provider_ledger}-wal"),
            ProtectedObjectKind.FILE,
        ),
        ProtectedRootBinding(
            ProtectedRootRole.PROVIDER_LEDGER_SHM,
            Path(f"{provider_ledger}-shm"),
            ProtectedObjectKind.FILE,
        ),
        ProtectedRootBinding(
            ProtectedRootRole.PROVIDER_LEDGER_PAYLOADS,
            provider_payloads,
            ProtectedObjectKind.DIRECTORY,
        ),
        ProtectedRootBinding(
            ProtectedRootRole.PAYLOAD_MUTATION_LEDGER,
            payload_mutation_ledger,
            ProtectedObjectKind.FILE,
        ),
        ProtectedRootBinding(
            ProtectedRootRole.PROVIDER_STATE,
            history_root,
            ProtectedObjectKind.DIRECTORY,
        ),
        ProtectedRootBinding(
            ProtectedRootRole.CALENDAR_DATABASE,
            calendar_database,
            ProtectedObjectKind.FILE,
        ),
        ProtectedRootBinding(
            ProtectedRootRole.CALENDAR_DATABASE_WAL,
            Path(f"{calendar_database}-wal"),
            ProtectedObjectKind.FILE,
        ),
        ProtectedRootBinding(
            ProtectedRootRole.CALENDAR_DATABASE_SHM,
            Path(f"{calendar_database}-shm"),
            ProtectedObjectKind.FILE,
        ),
        ProtectedRootBinding(
            ProtectedRootRole.CALENDAR_STATE,
            history_root,
            ProtectedObjectKind.DIRECTORY,
        ),
        ProtectedRootBinding(
            ProtectedRootRole.CALENDAR_PAYLOADS,
            payload_root,
            ProtectedObjectKind.DIRECTORY,
        ),
        ProtectedRootBinding(
            ProtectedRootRole.CHECKPOINTS,
            repository_root / "data" / "cache" / "checkpoints",
            ProtectedObjectKind.DIRECTORY,
        ),
        ProtectedRootBinding(
            ProtectedRootRole.REPORTS_LOGS,
            repository_root / "logs",
            ProtectedObjectKind.DIRECTORY,
        ),
        ProtectedRootBinding(
            ProtectedRootRole.TICKET07_EVIDENCE,
            evidence_root / "ticket07-evidence",
            ProtectedObjectKind.DIRECTORY,
        ),
        ProtectedRootBinding(
            ProtectedRootRole.PRIOR_INVENTORIES,
            evidence_root / "ticket06-post-cal-evidence",
            ProtectedObjectKind.DIRECTORY,
        ),
        ProtectedRootBinding(
            ProtectedRootRole.FAKE_YAHOO_INCIDENTS,
            provider_ledger,
            ProtectedObjectKind.FILE,
        ),
        ProtectedRootBinding(
            ProtectedRootRole.ISOLATED_PYTHON_WRITE_PROBE,
            payload_root / "isolated-python-write-probe",
            ProtectedObjectKind.FILE,
        ),
        ProtectedRootBinding(
            ProtectedRootRole.PRIOR_SANDBOX_QUALIFICATIONS,
            prior_sandbox_qualifications,
            ProtectedObjectKind.DIRECTORY,
        ),
        ProtectedRootBinding(
            ProtectedRootRole.REPLACEMENT_SANDBOX_QUALIFICATION,
            replacement_sandbox_qualification,
            ProtectedObjectKind.DIRECTORY,
        ),
        ProtectedRootBinding(
            ProtectedRootRole.QUALIFIED_REPLACEMENT_SANDBOX_QUALIFICATION,
            qualified_replacement_sandbox_qualification,
            ProtectedObjectKind.DIRECTORY,
        ),
        ProtectedRootBinding(
            ProtectedRootRole.FAILED_ISO04_TICKET06_ACCEPTANCE,
            failed_iso04_ticket06_acceptance,
            ProtectedObjectKind.DIRECTORY,
        ),
        ProtectedRootBinding(
            ProtectedRootRole.REVIEW_INVALIDATED_SANDBOX_QUALIFICATION,
            review_invalidated_sandbox_qualification,
            ProtectedObjectKind.DIRECTORY,
        ),
    )


class SourceManifestEntry(BaseModel):
    model_config = _CLOSED_MODEL_CONFIG

    relative_path: str = Field(min_length=1, max_length=512)
    git_mode: str = Field(pattern=r"^100(?:644|755)$")
    git_object_id: str = Field(pattern=r"^[0-9a-f]{40,64}$")
    byte_length: int = Field(ge=0)
    sha256: str = Field(pattern=r"^[0-9a-f]{64}$")


class ReviewedSourceIdentity(BaseModel):
    model_config = _CLOSED_MODEL_CONFIG

    contract_version: str = SOURCE_INPUT_CONTRACT_VERSION
    commit_id: str = Field(pattern=r"^[0-9a-f]{40,64}$")
    tree_hash: str = Field(pattern=r"^[0-9a-f]{40,64}$")
    git_object_format: str = Field(pattern=r"^sha(?:1|256)$")
    commit_object_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    archive_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    source_manifest_identity: str = Field(
        pattern=r"^windows-sandbox-source-manifest:v1:sha256:[0-9a-f]{64}$"
    )
    source_input_identity: str = Field(
        pattern=r"^windows-sandbox-reviewed-source:v1:sha256:[0-9a-f]{64}$"
    )
    tracked_file_count: int = Field(ge=1)
    run_live_environment: tuple[str, ...]

    @model_validator(mode="after")
    def _validate_source_identity(self) -> ReviewedSourceIdentity:
        payload = self.model_dump(mode="json", exclude={"source_input_identity"})
        if self.source_input_identity != _identity(
            "windows-sandbox-reviewed-source:v1",
            payload,
        ):
            raise ValueError("reviewed source identity mismatch")
        if tuple(sorted(set(self.run_live_environment))) != self.run_live_environment:
            raise ValueError("RUN_LIVE environment names must be sorted and unique")
        return self


class ReviewedSourceManifest(BaseModel):
    model_config = _CLOSED_MODEL_CONFIG

    contract_version: str = SOURCE_INPUT_CONTRACT_VERSION
    commit_id: str = Field(pattern=r"^[0-9a-f]{40,64}$")
    tree_hash: str = Field(pattern=r"^[0-9a-f]{40,64}$")
    git_object_format: str = Field(pattern=r"^sha(?:1|256)$")
    entries: tuple[SourceManifestEntry, ...]
    run_live_environment: tuple[str, ...]
    source_manifest_identity: str = Field(
        pattern=r"^windows-sandbox-source-manifest:v1:sha256:[0-9a-f]{64}$"
    )

    @model_validator(mode="after")
    def _validate_manifest(self) -> ReviewedSourceManifest:
        if tuple(entry.relative_path for entry in self.entries) != tuple(
            sorted(entry.relative_path for entry in self.entries)
        ):
            raise ValueError("source manifest entries must use canonical ordering")
        payload = self.model_dump(mode="json", exclude={"source_manifest_identity"})
        if self.source_manifest_identity != _identity(
            "windows-sandbox-source-manifest:v1",
            payload,
        ):
            raise ValueError("source manifest identity mismatch")
        return self


@dataclass(frozen=True, slots=True)
class StagedSourceInput:
    identity: ReviewedSourceIdentity
    manifest: ReviewedSourceManifest
    archive_path: Path
    manifest_path: Path
    commit_object_path: Path


def _run_git(
    repository_root: Path,
    arguments: tuple[str, ...],
    *,
    check: bool = True,
) -> subprocess.CompletedProcess[bytes]:
    environment = {
        name: value for name, value in os.environ.items() if not name.upper().startswith("GIT_")
    }
    environment.update(
        {
            "GIT_TERMINAL_PROMPT": "0",
        }
    )
    try:
        return subprocess.run(
            [
                "git",
                "-c",
                "core.fsmonitor=false",
                "-c",
                "core.hooksPath=NUL",
                *arguments,
            ],
            cwd=repository_root,
            env=environment,
            check=check,
            capture_output=True,
            shell=False,
        )
    except (OSError, subprocess.CalledProcessError) as exc:
        raise SandboxContractError(SandboxFailureReason.SOURCE_REPOSITORY_INVALID) from exc


def _git_text(repository_root: Path, *arguments: str) -> str:
    completed = _run_git(repository_root, tuple(arguments))
    try:
        return completed.stdout.decode("ascii").strip()
    except UnicodeDecodeError as exc:
        raise SandboxContractError(SandboxFailureReason.SOURCE_REPOSITORY_INVALID) from exc


def _forbidden_input_name(relative_path: str) -> bool:
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
    if any(part in {".aws", ".azure", ".kube", "gcloud", "credential stores"} for part in parts):
        return True
    database_name = name.removesuffix("-wal").removesuffix("-shm")
    if database_name.endswith((".db", ".sqlite", ".sqlite3")):
        return True
    return name.endswith((".log", ".cookie", ".cookies", ".credentials"))


def _credential_value_is_placeholder(value: str) -> bool:
    normalized = value.strip().strip("'\"").casefold()
    if not normalized:
        return True
    if normalized in {
        "changeme",
        "field(",
        "getenv(",
        "none",
        "null",
        "os.environ.get(",
        "os.getenv(",
        "secretstr(",
        "...",
        "x",
        "xxx",
    }:
        return True
    if _PLACEHOLDER_LITERAL_PATTERN.fullmatch(normalized):
        return True
    if re.fullmatch(r"your[-_](?:api[-_]?)?(?:key|password|secret|token)", normalized):
        return True
    if re.fullmatch(r"\$\{[A-Z_][A-Z0-9_]*\}", value.strip()):
        return True
    return bool(re.fullmatch(r"\{\{\s*[A-Za-z_][A-Za-z0-9_.-]*\s*\}\}", value.strip()))


def _test_fixture_credential_value_is_placeholder(value: str) -> bool:
    normalized = value.strip().strip("'\"").casefold()
    return bool(
        _TEST_FIXTURE_LITERAL_PATTERN.fullmatch(normalized)
        or sha256(value.encode("utf-8")).hexdigest() in _TEST_FIXTURE_LITERAL_SHA256
    )


def _test_source_known_secret_is_placeholder(value: str, source_text: str) -> bool:
    if "PRIVATE KEY" in value:
        blocks = re.findall(
            r"-----BEGIN[^\r\n]*PRIVATE KEY-----.*?"
            r"-----END[^\r\n]*PRIVATE KEY-----",
            source_text,
            flags=re.DOTALL,
        )
        return any(
            value in block and sha256(block.encode("utf-8")).hexdigest() in _TEST_FIXTURE_PEM_SHA256
            for block in blocks
        )
    body = re.sub(r"^(?:AKIA|gh[a-z]_|sk-)", "", value, flags=re.IGNORECASE)
    sequences = (
        "abcdefghijklmnopqrstuvwxyz",
        "ABCDEFGHIJKLMNOPQRSTUVWXYZ",
        "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ",
        "0123456789",
        "abcdefghijklmnopqrstuvwxyz0123456789",
        "ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789",
    )
    return bool(
        re.fullmatch(r"[A-Z0-9]{9}EXAMPLE", body)
        or any(body in sequence or body in sequence * 2 for sequence in sequences)
    )


def _test_source_credential_url_is_placeholder(value: str) -> bool:
    authority = value.rsplit("@", maxsplit=1)[-1].casefold()
    host = authority.rsplit(":", maxsplit=1)[0]
    return host.endswith((".invalid", ".test")) or host in {"invalid", "test"}


def _is_safe_hash_locked_requirement(line: str) -> bool:
    match = _HASH_LOCKED_REQUIREMENT_PATTERN.fullmatch(line)
    if match is None:
        return False
    raw_version = match.group("version")
    try:
        version = Version(raw_version)
    except InvalidVersion:
        return False
    return version.local is None and str(version) == raw_version


def _json_contains_credential(value: object) -> bool:
    if isinstance(value, dict):
        for key, item in value.items():
            if _CREDENTIAL_KEY_PATTERN.fullmatch(str(key)) and not (
                item is None
                or item == ""
                or isinstance(item, str)
                and _credential_value_is_placeholder(item)
            ):
                return True
            if _json_contains_credential(item):
                return True
    elif isinstance(value, list):
        return any(_json_contains_credential(item) for item in value)
    return False


def _validate_input_content(relative_path: str, content: bytes) -> None:
    if _forbidden_input_name(relative_path):
        raise SandboxContractError(SandboxFailureReason.FORBIDDEN_INPUT)
    text = content.decode("utf-8", errors="ignore")
    normalized_path = relative_path.replace("\\", "/").casefold()
    is_python_source = normalized_path.endswith(".py")
    is_python_test = is_python_source and normalized_path.startswith("tests/")
    stripped = text.lstrip()
    if stripped.startswith(("{", "[")):
        try:
            json_value = json.loads(text)
        except json.JSONDecodeError:
            json_value = None
        if json_value is not None and _json_contains_credential(json_value):
            raise SandboxContractError(SandboxFailureReason.CREDENTIAL_INPUT)
    credential_urls = tuple(_CREDENTIAL_URL_PATTERN.finditer(text))
    if credential_urls and not (
        is_python_test
        and all(
            _test_source_credential_url_is_placeholder(match.group(0)) for match in credential_urls
        )
    ):
        raise SandboxContractError(SandboxFailureReason.CREDENTIAL_INPUT)
    known_secrets = tuple(_KNOWN_SECRET_PATTERN.finditer(text))
    if known_secrets and not (
        is_python_test
        and all(
            _test_source_known_secret_is_placeholder(match.group(0), text)
            for match in known_secrets
        )
    ):
        raise SandboxContractError(SandboxFailureReason.CREDENTIAL_INPUT)
    if normalized_path == "scripts/windows_sandbox/iso02-requirements-win-cp313.txt":
        lines = text.splitlines()
        if not lines or not all(_is_safe_hash_locked_requirement(line) for line in lines):
            raise SandboxContractError(SandboxFailureReason.FORBIDDEN_INPUT)
        return
    for match in _CREDENTIAL_ASSIGNMENT_PATTERN.finditer(text):
        group_index, value = next(
            (index, group)
            for index, group in enumerate(match.groups(), start=1)
            if group is not None
        )
        if _credential_value_is_placeholder(value):
            continue
        if is_python_source and group_index == 3:
            continue
        if is_python_test and _test_fixture_credential_value_is_placeholder(value):
            continue
        raise SandboxContractError(SandboxFailureReason.CREDENTIAL_INPUT)


def _parse_source_entries(repository_root: Path) -> tuple[SourceManifestEntry, ...]:
    raw = _run_git(
        repository_root,
        ("ls-tree", "-r", "-z", "--full-tree", "HEAD"),
    ).stdout
    entries: list[SourceManifestEntry] = []
    for record in raw.split(b"\0"):
        if not record:
            continue
        try:
            metadata, path_bytes = record.split(b"\t", 1)
            mode_bytes, object_type, object_id_bytes = metadata.split(b" ", 2)
            relative_path = path_bytes.decode("utf-8")
            mode = mode_bytes.decode("ascii")
            object_id = object_id_bytes.decode("ascii")
        except (ValueError, UnicodeDecodeError) as exc:
            raise SandboxContractError(SandboxFailureReason.SOURCE_UNSUPPORTED_ENTRY) from exc
        if object_type != b"blob" or mode not in {"100644", "100755"}:
            raise SandboxContractError(SandboxFailureReason.SOURCE_UNSUPPORTED_ENTRY)
        path = Path(relative_path)
        if path.is_absolute() or ".." in path.parts or "\\" in relative_path:
            raise SandboxContractError(SandboxFailureReason.SOURCE_UNSUPPORTED_ENTRY)
        content = _run_git(repository_root, ("cat-file", "blob", object_id)).stdout
        _validate_input_content(relative_path, content)
        entries.append(
            SourceManifestEntry(
                relative_path=relative_path,
                git_mode=mode,
                git_object_id=object_id,
                byte_length=len(content),
                sha256=sha256(content).hexdigest(),
            )
        )
    if not entries:
        raise SandboxContractError(SandboxFailureReason.SOURCE_REPOSITORY_INVALID)
    entries.sort(key=lambda entry: entry.relative_path)
    return tuple(entries)


def _git_object_digest(object_format: str, object_type: str, content: bytes) -> str:
    algorithm = "sha1" if object_format == "sha1" else "sha256"
    digest = __import__("hashlib").new(algorithm)
    digest.update(f"{object_type} {len(content)}\0".encode("ascii"))
    digest.update(content)
    return digest.hexdigest()


def _tree_hash_from_entries(
    entries: tuple[SourceManifestEntry, ...],
    *,
    object_format: str,
) -> str:
    tree: dict[str, object] = {}
    for entry in entries:
        parts = entry.relative_path.split("/")
        node = tree
        for part in parts[:-1]:
            child = node.setdefault(part, {})
            if not isinstance(child, dict):
                raise SandboxContractError(SandboxFailureReason.SOURCE_IDENTITY_MISMATCH)
            node = child
        if parts[-1] in node:
            raise SandboxContractError(SandboxFailureReason.SOURCE_IDENTITY_MISMATCH)
        node[parts[-1]] = entry

    def hash_tree(node: dict[str, object]) -> str:
        material: list[tuple[bytes, bytes]] = []
        for name, value in node.items():
            encoded_name = name.encode("utf-8")
            if isinstance(value, dict):
                object_id = hash_tree(value)
                mode = b"40000"
                sort_key = encoded_name + b"/"
            else:
                assert isinstance(value, SourceManifestEntry)
                object_id = value.git_object_id
                mode = value.git_mode.encode("ascii")
                sort_key = encoded_name
            entry_bytes = mode + b" " + encoded_name + b"\0" + bytes.fromhex(object_id)
            material.append((sort_key, entry_bytes))
        body = b"".join(value for _key, value in sorted(material, key=lambda item: item[0]))
        return _git_object_digest(object_format, "tree", body)

    return hash_tree(tree)


def _write_deterministic_source_archive(
    repository_root: Path,
    entries: tuple[SourceManifestEntry, ...],
    archive_path: Path,
) -> None:
    directories = {
        "/".join(entry.relative_path.split("/")[:index])
        for entry in entries
        for index in range(1, len(entry.relative_path.split("/")))
    }
    with tarfile.open(archive_path, mode="x:", format=tarfile.GNU_FORMAT) as archive:
        for directory in sorted(directories):
            info = tarfile.TarInfo(directory)
            info.type = tarfile.DIRTYPE
            info.mode = 0o755
            info.mtime = 0
            info.uid = info.gid = 0
            info.uname = info.gname = ""
            archive.addfile(info)
        for entry in entries:
            content = _run_git(
                repository_root,
                ("cat-file", "blob", entry.git_object_id),
            ).stdout
            if sha256(content).hexdigest() != entry.sha256:
                raise SandboxContractError(SandboxFailureReason.SOURCE_IDENTITY_MISMATCH)
            info = tarfile.TarInfo(entry.relative_path)
            info.size = len(content)
            info.mode = int(entry.git_mode[-3:], 8)
            info.mtime = 0
            info.uid = info.gid = 0
            info.uname = info.gname = ""
            archive.addfile(info, io.BytesIO(content))


def _stage_reviewed_head_objects(
    repository: Path,
    destination: Path,
    *,
    commit_id: str,
    tree_hash: str,
    object_format: str,
) -> StagedSourceInput:
    entries = _parse_source_entries(repository)
    computed_tree = _tree_hash_from_entries(entries, object_format=object_format)
    if computed_tree != tree_hash:
        raise SandboxContractError(SandboxFailureReason.SOURCE_IDENTITY_MISMATCH)
    run_live_environment = tuple(
        sorted(
            {
                match.group(0).decode("ascii")
                for entry in entries
                for match in _RUN_LIVE_PATTERN.finditer(
                    _run_git(repository, ("cat-file", "blob", entry.git_object_id)).stdout
                )
            }
        )
    )
    manifest_payload = {
        "contract_version": SOURCE_INPUT_CONTRACT_VERSION,
        "commit_id": commit_id,
        "tree_hash": tree_hash,
        "git_object_format": object_format,
        "entries": [entry.model_dump(mode="json") for entry in entries],
        "run_live_environment": list(run_live_environment),
    }
    manifest = ReviewedSourceManifest(
        **manifest_payload,
        source_manifest_identity=_identity(
            "windows-sandbox-source-manifest:v1",
            manifest_payload,
        ),
    )

    try:
        destination.mkdir(parents=False, exist_ok=False)
    except OSError as exc:
        raise SandboxContractError(SandboxFailureReason.FORBIDDEN_INPUT) from exc
    archive_path = destination / "reviewed-source.tar"
    manifest_path = destination / "source-manifest.json"
    commit_object_path = destination / "reviewed-commit.object"
    _write_deterministic_source_archive(repository, entries, archive_path)
    commit_object = _run_git(repository, ("cat-file", "commit", commit_id)).stdout
    _validate_input_content("reviewed-commit.object", commit_object)
    if _git_object_digest(object_format, "commit", commit_object) != commit_id:
        raise SandboxContractError(SandboxFailureReason.SOURCE_IDENTITY_MISMATCH)
    if not commit_object.startswith(f"tree {tree_hash}\n".encode("ascii")):
        raise SandboxContractError(SandboxFailureReason.SOURCE_IDENTITY_MISMATCH)
    commit_object_path.write_bytes(commit_object)
    manifest_path.write_bytes(_canonical_json_bytes(manifest.model_dump(mode="json")) + b"\n")
    identity_payload = {
        "contract_version": SOURCE_INPUT_CONTRACT_VERSION,
        "commit_id": commit_id,
        "tree_hash": tree_hash,
        "git_object_format": object_format,
        "commit_object_sha256": sha256(commit_object).hexdigest(),
        "archive_sha256": sha256(archive_path.read_bytes()).hexdigest(),
        "source_manifest_identity": manifest.source_manifest_identity,
        "tracked_file_count": len(entries),
        "run_live_environment": list(run_live_environment),
    }
    identity = ReviewedSourceIdentity(
        **identity_payload,
        source_input_identity=_identity(
            "windows-sandbox-reviewed-source:v1",
            identity_payload,
        ),
    )
    return StagedSourceInput(
        identity=identity,
        manifest=manifest,
        archive_path=archive_path,
        manifest_path=manifest_path,
        commit_object_path=commit_object_path,
    )


def stage_reviewed_head_objects(
    repository_root: Path,
    destination: Path,
) -> StagedSourceInput:
    """Stage only reviewed HEAD Git objects, independent of worktree dirt/untracked state.

    ISO-02 uses this to bind a prepared bundle to a committed source identity.
    ISO-01 launch construction continues to call :func:`stage_reviewed_source`
    and retains its stricter clean-worktree preflight.
    """

    repository = repository_root.expanduser().resolve(strict=True)
    if _git_text(repository, "rev-parse", "--is-inside-work-tree") != "true":
        raise SandboxContractError(SandboxFailureReason.SOURCE_REPOSITORY_INVALID)
    commit_id = _git_text(repository, "rev-parse", "HEAD")
    tree_hash = _git_text(repository, "rev-parse", "HEAD^{tree}")
    object_format = _git_text(repository, "rev-parse", "--show-object-format")
    if object_format != "sha1":
        raise SandboxContractError(SandboxFailureReason.SOURCE_REPOSITORY_INVALID)
    return _stage_reviewed_head_objects(
        repository,
        destination,
        commit_id=commit_id,
        tree_hash=tree_hash,
        object_format=object_format,
    )


def stage_reviewed_source(
    repository_root: Path,
    destination: Path,
) -> StagedSourceInput:
    """Seal raw reviewed Git blobs without hooks, filters, or project imports."""

    repository = repository_root.expanduser().resolve(strict=True)
    if _git_text(repository, "rev-parse", "--is-inside-work-tree") != "true":
        raise SandboxContractError(SandboxFailureReason.SOURCE_REPOSITORY_INVALID)
    commit_id = _git_text(repository, "rev-parse", "HEAD")
    tree_hash = _git_text(repository, "rev-parse", "HEAD^{tree}")
    object_format = _git_text(repository, "rev-parse", "--show-object-format")
    if object_format != "sha1":
        raise SandboxContractError(SandboxFailureReason.SOURCE_REPOSITORY_INVALID)
    tracked_diff = _run_git(repository, ("diff", "--quiet", "HEAD", "--"), check=False)
    if tracked_diff.returncode not in {0, 1}:
        raise SandboxContractError(SandboxFailureReason.SOURCE_REPOSITORY_INVALID)
    if tracked_diff.returncode == 1:
        raise SandboxContractError(SandboxFailureReason.SOURCE_NOT_CLEAN)
    if _git_text(repository, "write-tree") != tree_hash:
        raise SandboxContractError(SandboxFailureReason.SOURCE_INDEX_MISMATCH)

    untracked = _run_git(
        repository,
        ("ls-files", "--others", "--exclude-standard", "-z"),
    ).stdout
    for raw_path in untracked.split(b"\0"):
        if not raw_path:
            continue
        try:
            relative_path = raw_path.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise SandboxContractError(SandboxFailureReason.FORBIDDEN_INPUT) from exc
        if _forbidden_input_name(relative_path):
            raise SandboxContractError(SandboxFailureReason.FORBIDDEN_INPUT)
        candidate = repository / relative_path
        try:
            metadata = candidate.lstat()
        except OSError as exc:
            raise SandboxContractError(SandboxFailureReason.FORBIDDEN_INPUT) from exc
        if (
            not stat.S_ISREG(metadata.st_mode)
            or stat.S_ISLNK(metadata.st_mode)
            or bool(getattr(metadata, "st_file_attributes", 0) & _WINDOWS_REPARSE_POINT_ATTRIBUTE)
            or metadata.st_size > 64 * 1024 * 1024
        ):
            raise SandboxContractError(SandboxFailureReason.FORBIDDEN_INPUT)
        _validate_input_content(relative_path, candidate.read_bytes())

    ignored_sensitive = _run_git(
        repository,
        (
            "ls-files",
            "--others",
            "--ignored",
            "--exclude-standard",
            "-z",
            "--",
            ".env",
            ".env.enterprise",
            ".netrc",
            ".pypirc",
            "pip.ini",
            "credentials.json",
            "cookies.sqlite",
            ":(glob)**/.env",
            ":(glob)**/.env.enterprise",
            ":(glob)**/.netrc",
            ":(glob)**/.pypirc",
            ":(glob)**/pip.ini",
            ":(glob)**/credentials.json",
            ":(glob)**/cookies.sqlite",
        ),
    ).stdout
    if ignored_sensitive.strip(b"\0"):
        raise SandboxContractError(SandboxFailureReason.FORBIDDEN_INPUT)

    return _stage_reviewed_head_objects(
        repository,
        destination,
        commit_id=commit_id,
        tree_hash=tree_hash,
        object_format=object_format,
    )


def _windows_normalized(value: str) -> str:
    return str(PureWindowsPath(value)).replace("/", "\\").casefold()


def _windows_is_within(value: str, root: str) -> bool:
    candidate = _windows_normalized(value)
    parent = _windows_normalized(root).rstrip("\\")
    return candidate == parent or candidate.startswith(parent + "\\")


class GuestLayout(BaseModel):
    model_config = _CLOSED_MODEL_CONFIG

    contract_version: str = GUEST_LAYOUT_CONTRACT_VERSION
    guest_root: str
    workspace_repository: str
    python_install: str
    virtual_environment: str
    home: str
    userprofile: str
    appdata: str
    localappdata: str
    temp: str
    tmp: str
    tradingagents_data: str
    tradingagents_cache: str
    market_history: str
    checkpoints: str
    reports: str
    logs: str
    payloads: str
    python_cache: str
    pip_cache: str
    pytest_temp: str
    guest_output: str
    layout_identity: str = Field(pattern=r"^windows-sandbox-guest-layout:v1:sha256:[0-9a-f]{64}$")

    def writable_paths(self) -> tuple[str, ...]:
        return (
            self.workspace_repository,
            self.python_install,
            self.virtual_environment,
            self.home,
            self.userprofile,
            self.appdata,
            self.localappdata,
            self.temp,
            self.tmp,
            self.tradingagents_data,
            self.tradingagents_cache,
            self.market_history,
            self.checkpoints,
            self.reports,
            self.logs,
            self.payloads,
            self.python_cache,
            self.pip_cache,
            self.pytest_temp,
            self.guest_output,
        )

    @model_validator(mode="after")
    def _validate_layout(self) -> GuestLayout:
        root = PureWindowsPath(self.guest_root)
        if not root.is_absolute() or _windows_normalized(self.guest_root) != r"c:\qcf17":
            raise ValueError("guest layout root must be the pinned Sandbox root")
        for value in self.writable_paths():
            path = PureWindowsPath(value)
            if not path.is_absolute() or not _windows_is_within(value, self.guest_root):
                raise ValueError("guest writable path escapes the guest root")
            normalized = _windows_normalized(value)
            if normalized.startswith(r"c:\users") or _windows_is_within(
                value,
                r"C:\QCF17\Input",
            ):
                raise ValueError("guest writable path resolves to a forbidden root")
        payload = self.model_dump(mode="json", exclude={"layout_identity"})
        if self.layout_identity != _identity(
            "windows-sandbox-guest-layout:v1",
            payload,
        ):
            raise ValueError("guest layout identity mismatch")
        return self


def build_guest_layout() -> GuestLayout:
    payload = {
        "contract_version": GUEST_LAYOUT_CONTRACT_VERSION,
        "guest_root": r"C:\QCF17",
        "workspace_repository": r"C:\QCF17\Work\Repository",
        "python_install": r"C:\QCF17\Python",
        "virtual_environment": r"C:\QCF17\Venv",
        "home": r"C:\QCF17\Runtime\Profile",
        "userprofile": r"C:\QCF17\Runtime\Profile",
        "appdata": r"C:\QCF17\Runtime\Profile\AppData\Roaming",
        "localappdata": r"C:\QCF17\Runtime\Profile\AppData\Local",
        "temp": r"C:\QCF17\Runtime\Temp",
        "tmp": r"C:\QCF17\Runtime\Temp",
        "tradingagents_data": r"C:\QCF17\Runtime\TradingAgents\Data",
        "tradingagents_cache": r"C:\QCF17\Runtime\TradingAgents\Cache",
        "market_history": r"C:\QCF17\Runtime\TradingAgents\History",
        "checkpoints": r"C:\QCF17\Runtime\TradingAgents\Cache\checkpoints",
        "reports": r"C:\QCF17\Runtime\TradingAgents\Reports",
        "logs": r"C:\QCF17\Runtime\TradingAgents\Logs",
        "payloads": r"C:\QCF17\Runtime\TradingAgents\History\payloads",
        "python_cache": r"C:\QCF17\Runtime\PythonCache",
        "pip_cache": r"C:\QCF17\Runtime\PipCache",
        "pytest_temp": r"C:\QCF17\Runtime\PytestTemp",
        "guest_output": r"C:\QCF17\Output",
    }
    return GuestLayout(
        **payload,
        layout_identity=_identity("windows-sandbox-guest-layout:v1", payload),
    )


class EnvironmentPolicy(BaseModel):
    model_config = _CLOSED_MODEL_CONFIG

    contract_version: str = ENVIRONMENT_POLICY_CONTRACT_VERSION
    variables: tuple[tuple[str, str], ...]
    inherited_host_environment_permitted: bool = False
    credential_environment_permitted: bool = False
    policy_identity: str = Field(
        pattern=r"^windows-sandbox-environment-policy:v1:sha256:[0-9a-f]{64}$"
    )

    @model_validator(mode="after")
    def _validate_policy(self) -> EnvironmentPolicy:
        keys = tuple(key for key, _value in self.variables)
        if keys != tuple(sorted(set(keys))):
            raise ValueError("environment variables must be sorted and unique")
        values = dict(self.variables)
        if values.get("PYTHON_DOTENV_DISABLED") != "1":
            raise ValueError("dotenv must be disabled")
        if any(value != "0" for key, value in self.variables if key.startswith("RUN_LIVE_")):
            raise ValueError("live opt-ins must be disabled")
        payload = self.model_dump(mode="json", exclude={"policy_identity"})
        if self.policy_identity != _identity(
            "windows-sandbox-environment-policy:v1",
            payload,
        ):
            raise ValueError("environment policy identity mismatch")
        return self


def build_environment_policy(
    *,
    run_live_environment: tuple[str, ...],
    layout: GuestLayout,
) -> EnvironmentPolicy:
    live_names = tuple(sorted(set(run_live_environment)))
    if any(re.fullmatch(r"RUN_LIVE_[A-Z0-9_]+", name) is None for name in live_names):
        raise SandboxContractError(SandboxFailureReason.ENVIRONMENT_POLICY_INVALID)
    variables = {
        "APPDATA": layout.appdata,
        "HOME": layout.home,
        "LOCALAPPDATA": layout.localappdata,
        "PIP_CACHE_DIR": layout.pip_cache,
        "PIP_DISABLE_PIP_VERSION_CHECK": "1",
        "PIP_NO_INDEX": "1",
        "PYTEST_ADDOPTS": f"--basetemp={PureWindowsPath(layout.pytest_temp).as_posix()}",
        "PYTHONPYCACHEPREFIX": layout.python_cache,
        "PYTHONUSERBASE": str(PureWindowsPath(layout.python_install) / "UserBase"),
        "PYTHON_DOTENV_DISABLED": "1",
        "PYTHON_EGG_CACHE": str(PureWindowsPath(layout.python_cache) / "Eggs"),
        "PYTHONNOUSERSITE": "1",
        "TEMP": layout.temp,
        "TMP": layout.tmp,
        "TMPDIR": layout.tmp,
        "TRADINGAGENTS_CACHE_DIR": layout.tradingagents_cache,
        "TRADINGAGENTS_MARKET_HISTORY_BACKUP_ROOT": str(
            PureWindowsPath(layout.market_history) / "backups"
        ),
        "TRADINGAGENTS_MARKET_HISTORY_DATABASE_PATH": str(
            PureWindowsPath(layout.market_history) / "market_history.sqlite3"
        ),
        "TRADINGAGENTS_MARKET_HISTORY_PAYLOAD_ROOT": layout.payloads,
        "TRADINGAGENTS_MEMORY_LOG_PATH": str(
            PureWindowsPath(layout.tradingagents_data) / "memory" / "trading_memory.md"
        ),
        "TRADINGAGENTS_PROJECT_ROOT": str(PureWindowsPath(layout.tradingagents_data).parent),
        "TRADINGAGENTS_RESULTS_DIR": layout.reports,
        "USERPROFILE": layout.userprofile,
        "XDG_CACHE_HOME": layout.tradingagents_cache,
    }
    variables.update(dict.fromkeys(live_names, "0"))
    ordered = tuple(sorted(variables.items()))
    payload = {
        "contract_version": ENVIRONMENT_POLICY_CONTRACT_VERSION,
        "variables": [list(item) for item in ordered],
        "inherited_host_environment_permitted": False,
        "credential_environment_permitted": False,
    }
    return EnvironmentPolicy(
        variables=ordered,
        policy_identity=_identity("windows-sandbox-environment-policy:v1", payload),
    )


class SandboxMappingRole(str, Enum):
    SOURCE = "source"
    RUNNER = "runner"
    CONTROL = "control"
    RUNTIME_BUNDLE = "runtime_bundle"
    RESULTS_EXPORT = "results_export"


_PINNED_GUEST_MAPPING_PATHS = {
    SandboxMappingRole.SOURCE: r"C:\QCF17\Input\Source",
    SandboxMappingRole.RUNNER: r"C:\QCF17\Input\Runner",
    SandboxMappingRole.CONTROL: r"C:\QCF17\Input\Control",
    SandboxMappingRole.RUNTIME_BUNDLE: r"C:\QCF17\Input\Runtime",
    SandboxMappingRole.RESULTS_EXPORT: r"C:\QCF17\Export",
}


@dataclass(frozen=True, slots=True)
class SandboxMappingBinding:
    role: SandboxMappingRole
    host_path: Path
    guest_path: str
    read_only: bool
    content_identity: str
    fresh_empty: bool = False


class SandboxMapping(BaseModel):
    model_config = _CLOSED_MODEL_CONFIG

    role: SandboxMappingRole
    host_path_identity: str = Field(pattern=r"^host-path:sha256:[0-9a-f]{64}$")
    guest_path: str
    read_only: bool
    content_identity: str = Field(min_length=1, max_length=160)
    fresh_empty: bool = False


class SandboxMappingManifest(BaseModel):
    model_config = _CLOSED_MODEL_CONFIG

    contract_version: str = MAPPING_MANIFEST_CONTRACT_VERSION
    mappings: tuple[SandboxMapping, ...]
    mapping_manifest_identity: str = Field(
        pattern=r"^windows-sandbox-mapping-manifest:v1:sha256:[0-9a-f]{64}$"
    )

    @model_validator(mode="after")
    def _validate_manifest(self) -> SandboxMappingManifest:
        if tuple(mapping.role for mapping in self.mappings) != tuple(SandboxMappingRole):
            raise ValueError("mapping roles must use canonical closed ordering")
        payload = self.model_dump(mode="json", exclude={"mapping_manifest_identity"})
        if self.mapping_manifest_identity != _identity(
            "windows-sandbox-mapping-manifest:v1",
            payload,
        ):
            raise ValueError("mapping manifest identity mismatch")
        return self


@dataclass(frozen=True, slots=True)
class ResolvedSandboxMappingManifest:
    contract: SandboxMappingManifest
    bindings: tuple[SandboxMappingBinding, ...]

    @property
    def mappings(self) -> tuple[SandboxMapping, ...]:
        return self.contract.mappings

    @property
    def mapping_manifest_identity(self) -> str:
        return self.contract.mapping_manifest_identity


def _paths_overlap(first: Path, second: Path) -> bool:
    return first == second or first.is_relative_to(second) or second.is_relative_to(first)


def build_mapping_manifest(
    bindings: tuple[SandboxMappingBinding, ...],
    *,
    protected_manifest: ResolvedProtectedRootManifest,
    forbidden_host_roots: tuple[Path, ...],
) -> ResolvedSandboxMappingManifest:
    by_role: dict[SandboxMappingRole, SandboxMappingBinding] = {}
    for binding in bindings:
        if binding.role in by_role:
            raise SandboxContractError(SandboxFailureReason.MAPPING_CONTRACT_INVALID)
        by_role[binding.role] = binding
    if set(by_role) != set(SandboxMappingRole) or len(bindings) != len(SandboxMappingRole):
        writable_count = sum(not binding.read_only for binding in bindings)
        reason = (
            SandboxFailureReason.ADDITIONAL_WRITABLE_MAPPING
            if writable_count > 1
            else SandboxFailureReason.MAPPING_CONTRACT_INVALID
        )
        raise SandboxContractError(reason)

    resolved: list[SandboxMappingBinding] = []
    for role in SandboxMappingRole:
        binding = by_role[role]
        if _windows_normalized(binding.guest_path) != _windows_normalized(
            _PINNED_GUEST_MAPPING_PATHS[role]
        ):
            raise SandboxContractError(SandboxFailureReason.MAPPING_CONTRACT_INVALID)
        if role is SandboxMappingRole.RESULTS_EXPORT:
            if binding.read_only:
                raise SandboxContractError(SandboxFailureReason.MAPPING_CONTRACT_INVALID)
        elif not binding.read_only:
            raise SandboxContractError(SandboxFailureReason.WRITABLE_INPUT_MAPPING)
        try:
            host_path = binding.host_path.expanduser().resolve(strict=True)
        except OSError as exc:
            raise SandboxContractError(SandboxFailureReason.MAPPING_CONTRACT_INVALID) from exc
        if not host_path.is_dir() or host_path.is_symlink():
            raise SandboxContractError(SandboxFailureReason.MAPPING_CONTRACT_INVALID)
        if role is SandboxMappingRole.RESULTS_EXPORT:
            if not binding.fresh_empty or any(host_path.iterdir()):
                raise SandboxContractError(SandboxFailureReason.RESULTS_EXPORT_NOT_FRESH)
        elif binding.fresh_empty:
            raise SandboxContractError(SandboxFailureReason.MAPPING_CONTRACT_INVALID)
        for protected in protected_manifest.bindings:
            if _paths_overlap(host_path, protected.path):
                raise SandboxContractError(SandboxFailureReason.PROTECTED_HOST_MAPPING)
        for forbidden in forbidden_host_roots:
            forbidden_path = forbidden.expanduser().resolve(strict=False)
            if _paths_overlap(host_path, forbidden_path):
                raise SandboxContractError(SandboxFailureReason.FORBIDDEN_HOST_MAPPING)
        resolved.append(replace(binding, host_path=host_path))

    for index, first in enumerate(resolved):
        for second in resolved[index + 1 :]:
            if (
                _paths_overlap(first.host_path, second.host_path)
                or _windows_is_within(
                    first.guest_path,
                    second.guest_path,
                )
                or _windows_is_within(second.guest_path, first.guest_path)
            ):
                raise SandboxContractError(SandboxFailureReason.OVERLAPPING_MAPPING)
    if sum(not binding.read_only for binding in resolved) != 1:
        raise SandboxContractError(SandboxFailureReason.ADDITIONAL_WRITABLE_MAPPING)

    mappings = tuple(
        SandboxMapping(
            role=binding.role,
            host_path_identity=_safe_path_identity(binding.host_path),
            guest_path=str(PureWindowsPath(binding.guest_path)),
            read_only=binding.read_only,
            content_identity=binding.content_identity,
            fresh_empty=binding.fresh_empty,
        )
        for binding in resolved
    )
    payload = {
        "contract_version": MAPPING_MANIFEST_CONTRACT_VERSION,
        "mappings": [mapping.model_dump(mode="json") for mapping in mappings],
    }
    contract = SandboxMappingManifest(
        mappings=mappings,
        mapping_manifest_identity=_identity(
            "windows-sandbox-mapping-manifest:v1",
            payload,
        ),
    )
    return ResolvedSandboxMappingManifest(contract=contract, bindings=tuple(resolved))


class SandboxConfigurationPolicy(BaseModel):
    model_config = _CLOSED_MODEL_CONFIG

    contract_version: str = WSB_CONFIGURATION_CONTRACT_VERSION
    networking: str = "Disable"
    vgpu: str = "Disable"
    audio_input: str = "Disable"
    video_input: str = "Disable"
    printer_redirection: str = "Disable"
    clipboard_redirection: str = "Disable"
    protected_client: str
    protected_client_supported: bool
    logon_command: str
    policy_identity: str = Field(pattern=r"^windows-sandbox-config-policy:v1:sha256:[0-9a-f]{64}$")


@dataclass(frozen=True, slots=True)
class WsbConfiguration:
    contract_version: str
    policy: SandboxConfigurationPolicy
    xml: str
    config_identity: str


def render_wsb_configuration(
    mapping_manifest: ResolvedSandboxMappingManifest,
    *,
    protected_client_supported: bool,
) -> WsbConfiguration:
    command = (
        "powershell.exe -NoLogo -NoProfile -NonInteractive "
        '-ExecutionPolicy Bypass -File "C:\\QCF17\\Input\\Runner\\bootstrap.ps1"'
    )
    policy_payload = {
        "contract_version": WSB_CONFIGURATION_CONTRACT_VERSION,
        "networking": "Disable",
        "vgpu": "Disable",
        "audio_input": "Disable",
        "video_input": "Disable",
        "printer_redirection": "Disable",
        "clipboard_redirection": "Disable",
        "protected_client": "Enable" if protected_client_supported else "Unsupported",
        "protected_client_supported": protected_client_supported,
        "logon_command": command,
    }
    policy = SandboxConfigurationPolicy(
        **policy_payload,
        policy_identity=_identity("windows-sandbox-config-policy:v1", policy_payload),
    )
    lines = [
        '<?xml version="1.0" encoding="utf-8"?>',
        "<Configuration>",
        "  <vGPU>Disable</vGPU>",
        "  <Networking>Disable</Networking>",
        "  <AudioInput>Disable</AudioInput>",
        "  <VideoInput>Disable</VideoInput>",
        "  <PrinterRedirection>Disable</PrinterRedirection>",
        "  <ClipboardRedirection>Disable</ClipboardRedirection>",
    ]
    if protected_client_supported:
        lines.append("  <ProtectedClient>Enable</ProtectedClient>")
    lines.append("  <MappedFolders>")
    for binding in mapping_manifest.bindings:
        lines.extend(
            (
                "    <MappedFolder>",
                f"      <HostFolder>{xml_escape(str(binding.host_path))}</HostFolder>",
                f"      <SandboxFolder>{xml_escape(str(PureWindowsPath(binding.guest_path)))}</SandboxFolder>",
                f"      <ReadOnly>{str(binding.read_only).lower()}</ReadOnly>",
                "    </MappedFolder>",
            )
        )
    lines.extend(
        (
            "  </MappedFolders>",
            "  <LogonCommand>",
            f"    <Command>{xml_escape(command)}</Command>",
            "  </LogonCommand>",
            "</Configuration>",
            "",
        )
    )
    xml = "\n".join(lines)
    return WsbConfiguration(
        contract_version=WSB_CONFIGURATION_CONTRACT_VERSION,
        policy=policy,
        xml=xml,
        config_identity=_identity("windows-sandbox-config:v1", xml),
    )


class RuntimeBundleInputContract(BaseModel):
    """ISO-02 fill point; ISO-01 creates only the reviewed placeholder."""

    model_config = _CLOSED_MODEL_CONFIG

    contract_version: str = RUNTIME_BUNDLE_INPUT_CONTRACT_VERSION
    state: Literal["placeholder_for_iso_02", "sealed"]
    mapped_read_only: bool = True
    offline_required: bool = True
    network_install_permitted: bool = False
    hash_algorithm: Literal["sha256"] = "sha256"
    python_install_target: str
    virtual_environment_target: str
    bundle_manifest_identity: str | None = Field(
        default=None,
        max_length=180,
        pattern=r"^[a-z0-9][a-z0-9._:-]{0,110}:sha256:[0-9a-f]{64}$",
    )
    bundle_manifest_sha256: str | None = Field(
        default=None,
        pattern=r"^[0-9a-f]{64}$",
    )
    bootstrap_relative_path: str | None = Field(default=None, max_length=260)
    git_relative_path: str | None = Field(default=None, max_length=260)
    runtime_bundle_contract_identity: str = Field(
        pattern=r"^windows-sandbox-runtime-bundle-input:v1:sha256:[0-9a-f]{64}$"
    )

    @model_validator(mode="after")
    def _validate_runtime_bundle_contract(self) -> RuntimeBundleInputContract:
        if not self.mapped_read_only or not self.offline_required:
            raise ValueError("runtime bundle input must remain read-only and offline")
        if self.network_install_permitted:
            raise ValueError("runtime bundle cannot permit network installation")
        layout = build_guest_layout()
        if _windows_normalized(self.python_install_target) != _windows_normalized(
            layout.python_install
        ) or _windows_normalized(self.virtual_environment_target) != _windows_normalized(
            layout.virtual_environment
        ):
            raise ValueError("runtime bundle target must use the pinned guest layout")
        if self.state == "placeholder_for_iso_02" and any(
            (self.bundle_manifest_identity, self.bundle_manifest_sha256)
        ):
            raise ValueError("placeholder runtime bundle cannot claim a manifest")
        if self.state == "placeholder_for_iso_02" and any(
            (self.bootstrap_relative_path, self.git_relative_path)
        ):
            raise ValueError("placeholder runtime bundle cannot claim executables")
        for relative_path in (self.bootstrap_relative_path, self.git_relative_path):
            if relative_path is None:
                continue
            candidate = PureWindowsPath(relative_path)
            if (
                candidate.is_absolute()
                or candidate.drive
                or not candidate.parts
                or ".." in candidate.parts
                or any(part in {"", "."} for part in candidate.parts)
            ):
                raise ValueError("runtime bundle relative path must remain inside the mapping")
        if self.state == "sealed" and (
            self.bundle_manifest_identity is None
            or self.bundle_manifest_sha256 is None
            or self.bootstrap_relative_path is None
            or self.git_relative_path is None
        ):
            raise ValueError("sealed runtime bundle requires manifest and executable paths")
        payload = self.model_dump(
            mode="json",
            exclude={"runtime_bundle_contract_identity"},
        )
        if self.runtime_bundle_contract_identity != _identity(
            "windows-sandbox-runtime-bundle-input:v1",
            payload,
        ):
            raise ValueError("runtime bundle input identity mismatch")
        return self


def build_runtime_bundle_placeholder() -> RuntimeBundleInputContract:
    layout = build_guest_layout()
    payload = {
        "contract_version": RUNTIME_BUNDLE_INPUT_CONTRACT_VERSION,
        "state": "placeholder_for_iso_02",
        "mapped_read_only": True,
        "offline_required": True,
        "network_install_permitted": False,
        "hash_algorithm": "sha256",
        "python_install_target": layout.python_install,
        "virtual_environment_target": layout.virtual_environment,
        "bundle_manifest_identity": None,
        "bundle_manifest_sha256": None,
        "bootstrap_relative_path": None,
        "git_relative_path": None,
    }
    return RuntimeBundleInputContract(
        **payload,
        runtime_bundle_contract_identity=_identity(
            "windows-sandbox-runtime-bundle-input:v1",
            payload,
        ),
    )


def build_sealed_runtime_bundle_input(
    manifest: OfflineRuntimeBundleManifest,
    *,
    manifest_sha256: str,
) -> RuntimeBundleInputContract:
    """Fill ISO-01's reviewed mapping contract from a verified ISO-02 bundle."""

    layout = build_guest_layout()
    payload = {
        "contract_version": RUNTIME_BUNDLE_INPUT_CONTRACT_VERSION,
        "state": "sealed",
        "mapped_read_only": True,
        "offline_required": True,
        "network_install_permitted": False,
        "hash_algorithm": "sha256",
        "python_install_target": layout.python_install,
        "virtual_environment_target": layout.virtual_environment,
        "bundle_manifest_identity": manifest.bundle_identity,
        "bundle_manifest_sha256": manifest_sha256,
        "bootstrap_relative_path": manifest.bootstrap_relative_path,
        "git_relative_path": manifest.portable_git_relative_path,
    }
    return RuntimeBundleInputContract(
        **payload,
        runtime_bundle_contract_identity=_identity(
            "windows-sandbox-runtime-bundle-input:v1",
            payload,
        ),
    )


class RunnerIdentityMaterial(BaseModel):
    model_config = _CLOSED_MODEL_CONFIG

    runner_contract_version: str = RUNNER_CONTRACT_VERSION
    protected_root_manifest_identity: str = Field(min_length=1, max_length=180)
    source_input_identity: str = Field(min_length=1, max_length=180)
    wsb_config_identity: str = Field(min_length=1, max_length=180)
    mapping_manifest_identity: str = Field(min_length=1, max_length=180)
    guest_layout_identity: str = Field(min_length=1, max_length=180)
    environment_policy_identity: str = Field(min_length=1, max_length=180)
    result_schema_identity: str = Field(min_length=1, max_length=180)
    runtime_bundle_contract_identity: str = Field(min_length=1, max_length=180)
    runner_bootstrap_identity: str = Field(min_length=1, max_length=180)
    control_inventory_identity: str = Field(min_length=1, max_length=180)


def build_runner_identity(material: RunnerIdentityMaterial) -> str:
    return _identity(
        "iso01-windows-sandbox-runner:v1",
        material.model_dump(mode="json"),
    )


class SandboxExecutionMode(str, Enum):
    BOUNDARY_QUALIFICATION = "boundary_qualification"
    TICKET06_ACCEPTANCE = "ticket06_acceptance"


class ResultSectionStatus(str, Enum):
    NOT_RUN = "not_run"
    PASS = "pass"
    FAIL = "fail"


class BoundedResultSection(BaseModel):
    model_config = _CLOSED_MODEL_CONFIG

    status: ResultSectionStatus = ResultSectionStatus.NOT_RUN
    total: int = Field(default=0, ge=0, le=10_000)
    passed: int = Field(default=0, ge=0, le=10_000)
    failed: int = Field(default=0, ge=0, le=10_000)
    record_identities: tuple[str, ...] = Field(default=(), max_length=512)
    summary_identity: str | None = Field(default=None, max_length=180)

    @model_validator(mode="after")
    def _validate_counts(self) -> BoundedResultSection:
        if self.passed + self.failed > self.total:
            raise ValueError("bounded result counts are inconsistent")
        identities = (*self.record_identities, self.summary_identity)
        if any(
            identity is not None
            and (len(identity) > 180 or _MACHINE_IDENTITY_PATTERN.fullmatch(identity) is None)
            for identity in identities
        ):
            raise ValueError("bounded result identity must be a namespaced SHA-256 digest")
        if self.status is ResultSectionStatus.NOT_RUN and any(
            (
                self.total,
                self.passed,
                self.failed,
                len(self.record_identities),
                self.summary_identity is not None,
            )
        ):
            raise ValueError("not-run result section cannot carry execution data")
        if (
            self.status is ResultSectionStatus.PASS
            and self.failed != 0
            or self.status is ResultSectionStatus.FAIL
            and self.failed == 0
        ):
            raise ValueError("bounded result status and counts are inconsistent")
        return self


_RESULT_SCHEMA_DESCRIPTOR = {
    "contract_version": RESULT_SCHEMA_CONTRACT_VERSION,
    "allowed_export_files": ["completion.json", "result.json"],
    "execution_modes": [mode.value for mode in SandboxExecutionMode],
    "section_names": ["ac", "diff", "pe", "probes", "pytest", "qcf", "ruff"],
    "section_record_limit": 512,
    "dynamic_identity_pattern": _MACHINE_IDENTITY_PATTERN_TEXT,
    "status_count_consistency_required": True,
    "maximum_file_bytes": 262_144,
    "maximum_export_bytes": 524_288,
    "completion_written_last": True,
    "host_qcf17_authority_from_guest": False,
}
RESULT_SCHEMA_IDENTITY = _identity(
    "windows-sandbox-result-schema:v1",
    _RESULT_SCHEMA_DESCRIPTOR,
)
COMPLETION_MARKER_CONTRACT_IDENTITY = _identity(
    "windows-sandbox-completion-marker:v1",
    {
        "contract_version": COMPLETION_MARKER_CONTRACT_VERSION,
        "result_digest_algorithm": "sha256",
        "write_order": "last",
        "allowed_filename": "completion.json",
    },
)


class SandboxGuestResult(BaseModel):
    model_config = _CLOSED_MODEL_CONFIG

    contract_version: str = RESULT_SCHEMA_CONTRACT_VERSION
    result_schema_identity: str = RESULT_SCHEMA_IDENTITY
    execution_mode: SandboxExecutionMode
    runner_identity: str = Field(min_length=1, max_length=180)
    wsb_config_identity: str = Field(min_length=1, max_length=180)
    source_commit_id: str = Field(pattern=r"^[0-9a-f]{40,64}$")
    source_tree_hash: str = Field(pattern=r"^[0-9a-f]{40,64}$")
    source_archive_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    source_manifest_identity: str = Field(min_length=1, max_length=180)
    protected_root_manifest_identity: str = Field(min_length=1, max_length=180)
    runtime_bundle_contract_identity: str = Field(min_length=1, max_length=180)
    mapping_manifest_identity: str = Field(min_length=1, max_length=180)
    guest_workspace_identity: str = Field(min_length=1, max_length=180)
    completion_marker_contract_identity: str = COMPLETION_MARKER_CONTRACT_IDENTITY
    guest_claim: Literal["not_executed", "pass", "fail"] = "not_executed"
    probes: BoundedResultSection = BoundedResultSection()
    qcf: BoundedResultSection = BoundedResultSection()
    pe: BoundedResultSection = BoundedResultSection()
    ac: BoundedResultSection = BoundedResultSection()
    pytest: BoundedResultSection = BoundedResultSection()
    ruff: BoundedResultSection = BoundedResultSection()
    diff: BoundedResultSection = BoundedResultSection()
    result_identity: str = Field(pattern=r"^windows-sandbox-result:v1:sha256:[0-9a-f]{64}$")

    @model_validator(mode="after")
    def _validate_result_identity(self) -> SandboxGuestResult:
        if self.result_schema_identity != RESULT_SCHEMA_IDENTITY:
            raise ValueError("result schema identity mismatch")
        if self.completion_marker_contract_identity != COMPLETION_MARKER_CONTRACT_IDENTITY:
            raise ValueError("completion marker contract identity mismatch")
        payload = self.model_dump(mode="json", exclude={"result_identity"})
        if self.result_identity != _identity("windows-sandbox-result:v1", payload):
            raise ValueError("guest result identity mismatch")
        sections = (
            self.probes,
            self.qcf,
            self.pe,
            self.ac,
            self.pytest,
            self.ruff,
            self.diff,
        )
        if self.guest_claim == "not_executed" and any(
            section.status is not ResultSectionStatus.NOT_RUN for section in sections
        ):
            raise ValueError("non-executed guest result cannot carry executed sections")
        return self

    def canonical_json_bytes(self) -> bytes:
        return _canonical_json_bytes(self.model_dump(mode="json"))

    def static_binding_projection(self) -> dict[str, object]:
        return self.model_dump(
            mode="json",
            exclude={
                "guest_claim",
                "probes",
                "qcf",
                "pe",
                "ac",
                "pytest",
                "ruff",
                "diff",
                "result_identity",
            },
        )


def build_guest_result_template(
    *,
    execution_mode: SandboxExecutionMode,
    runner_identity: str,
    wsb_config_identity: str,
    source_commit_id: str,
    source_tree_hash: str,
    source_archive_sha256: str,
    source_manifest_identity: str,
    protected_root_manifest_identity: str,
    runtime_bundle_contract_identity: str,
    mapping_manifest_identity: str,
    guest_workspace_identity: str,
) -> SandboxGuestResult:
    section = BoundedResultSection()
    payload = {
        "contract_version": RESULT_SCHEMA_CONTRACT_VERSION,
        "result_schema_identity": RESULT_SCHEMA_IDENTITY,
        "execution_mode": execution_mode,
        "runner_identity": runner_identity,
        "wsb_config_identity": wsb_config_identity,
        "source_commit_id": source_commit_id,
        "source_tree_hash": source_tree_hash,
        "source_archive_sha256": source_archive_sha256,
        "source_manifest_identity": source_manifest_identity,
        "protected_root_manifest_identity": protected_root_manifest_identity,
        "runtime_bundle_contract_identity": runtime_bundle_contract_identity,
        "mapping_manifest_identity": mapping_manifest_identity,
        "guest_workspace_identity": guest_workspace_identity,
        "completion_marker_contract_identity": COMPLETION_MARKER_CONTRACT_IDENTITY,
        "guest_claim": "not_executed",
        "probes": section,
        "qcf": section,
        "pe": section,
        "ac": section,
        "pytest": section,
        "ruff": section,
        "diff": section,
    }
    json_payload = {
        key: value.model_dump(mode="json")
        if isinstance(value, BaseModel)
        else value.value
        if isinstance(value, Enum)
        else value
        for key, value in payload.items()
    }
    return SandboxGuestResult(
        **payload,
        result_identity=_identity("windows-sandbox-result:v1", json_payload),
    )


def finalize_guest_result(
    template: SandboxGuestResult,
    *,
    guest_claim: Literal["pass", "fail"],
    probes: BoundedResultSection | None = None,
    qcf: BoundedResultSection | None = None,
    pe: BoundedResultSection | None = None,
    ac: BoundedResultSection | None = None,
    pytest: BoundedResultSection | None = None,
    ruff: BoundedResultSection | None = None,
    diff: BoundedResultSection | None = None,
) -> SandboxGuestResult:
    """Populate bounded execution fields without weakening static run bindings."""

    if template.guest_claim != "not_executed":
        raise ValueError("guest result template was already finalized")
    payload = template.model_dump(mode="json", exclude={"result_identity"})
    payload.update(
        {
            "guest_claim": guest_claim,
            "probes": (probes or template.probes).model_dump(mode="json"),
            "qcf": (qcf or template.qcf).model_dump(mode="json"),
            "pe": (pe or template.pe).model_dump(mode="json"),
            "ac": (ac or template.ac).model_dump(mode="json"),
            "pytest": (pytest or template.pytest).model_dump(mode="json"),
            "ruff": (ruff or template.ruff).model_dump(mode="json"),
            "diff": (diff or template.diff).model_dump(mode="json"),
        }
    )
    return SandboxGuestResult(
        **payload,
        result_identity=_identity("windows-sandbox-result:v1", payload),
    )


class CompletionMarker(BaseModel):
    model_config = _CLOSED_MODEL_CONFIG

    contract_version: str = COMPLETION_MARKER_CONTRACT_VERSION
    completion_marker_contract_identity: str = COMPLETION_MARKER_CONTRACT_IDENTITY
    result_identity: str = Field(pattern=r"^windows-sandbox-result:v1:sha256:[0-9a-f]{64}$")
    result_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    write_order: Literal["last"] = "last"
    marker_identity: str = Field(pattern=r"^windows-sandbox-completion:v1:sha256:[0-9a-f]{64}$")

    @model_validator(mode="after")
    def _validate_marker(self) -> CompletionMarker:
        if self.completion_marker_contract_identity != COMPLETION_MARKER_CONTRACT_IDENTITY:
            raise ValueError("completion marker contract mismatch")
        payload = self.model_dump(mode="json", exclude={"marker_identity"})
        if self.marker_identity != _identity("windows-sandbox-completion:v1", payload):
            raise ValueError("completion marker identity mismatch")
        return self

    def canonical_json_bytes(self) -> bytes:
        return _canonical_json_bytes(self.model_dump(mode="json"))


def build_completion_marker(
    *,
    result: SandboxGuestResult,
    result_bytes: bytes,
) -> CompletionMarker:
    payload = {
        "contract_version": COMPLETION_MARKER_CONTRACT_VERSION,
        "completion_marker_contract_identity": COMPLETION_MARKER_CONTRACT_IDENTITY,
        "result_identity": result.result_identity,
        "result_sha256": sha256(result_bytes).hexdigest(),
        "write_order": "last",
    }
    return CompletionMarker(
        **payload,
        marker_identity=_identity("windows-sandbox-completion:v1", payload),
    )


@dataclass(frozen=True, slots=True)
class ValidatedResultExport:
    result: SandboxGuestResult
    completion: CompletionMarker
    export_identity: str
    host_qcf17_authority: bool = False


def validate_result_export(
    export_root: Path,
    *,
    expected: SandboxGuestResult,
) -> ValidatedResultExport:
    try:
        root = export_root.expanduser().resolve(strict=True)
        files = tuple(sorted(root.iterdir(), key=lambda path: path.name))
    except OSError as exc:
        raise SandboxContractError(SandboxFailureReason.UNSAFE_RESULT_EXPORT) from exc
    if not root.is_dir() or tuple(path.name for path in files) != (
        "completion.json",
        "result.json",
    ):
        raise SandboxContractError(SandboxFailureReason.UNSAFE_RESULT_EXPORT)
    total = 0
    for path in files:
        try:
            metadata = path.lstat()
        except OSError as exc:
            raise SandboxContractError(SandboxFailureReason.UNSAFE_RESULT_EXPORT) from exc
        if not stat.S_ISREG(metadata.st_mode) or stat.S_ISLNK(metadata.st_mode):
            raise SandboxContractError(SandboxFailureReason.UNSAFE_RESULT_EXPORT)
        if metadata.st_size > 262_144:
            raise SandboxContractError(SandboxFailureReason.UNSAFE_RESULT_EXPORT)
        total += metadata.st_size
    if total > 524_288:
        raise SandboxContractError(SandboxFailureReason.UNSAFE_RESULT_EXPORT)
    result_path = root / "result.json"
    completion_path = root / "completion.json"
    result_bytes = result_path.read_bytes()
    completion_bytes = completion_path.read_bytes()
    try:
        _validate_input_content("result.json", result_bytes)
        _validate_input_content("completion.json", completion_bytes)
        result = SandboxGuestResult.model_validate_json(result_bytes)
        completion = CompletionMarker.model_validate_json(completion_bytes)
    except SandboxContractError:
        raise
    except (ValueError, TypeError) as exc:
        raise SandboxContractError(SandboxFailureReason.COMPLETION_MARKER_INVALID) from exc
    if result.static_binding_projection() != expected.static_binding_projection():
        raise SandboxContractError(SandboxFailureReason.RESULT_IDENTITY_MISMATCH)
    if (
        completion.result_identity != result.result_identity
        or completion.result_sha256 != sha256(result_bytes).hexdigest()
        or completion_path.stat().st_mtime_ns < result_path.stat().st_mtime_ns
    ):
        raise SandboxContractError(SandboxFailureReason.COMPLETION_MARKER_INVALID)
    export_payload = {
        "result_identity": result.result_identity,
        "completion_identity": completion.marker_identity,
        "result_sha256": sha256(result_bytes).hexdigest(),
        "completion_sha256": sha256(completion_bytes).hexdigest(),
    }
    return ValidatedResultExport(
        result=result,
        completion=completion,
        export_identity=_identity("windows-sandbox-result-export:v1", export_payload),
    )


_REQUIRED_WSB_SETTINGS = (
    "AudioInput",
    "ClipboardRedirection",
    "LogonCommand",
    "MappedFolders",
    "Networking",
    "PrinterRedirection",
    "vGPU",
    "VideoInput",
)


class SandboxPlatformReport(BaseModel):
    model_config = _CLOSED_MODEL_CONFIG

    contract_version: str = "windows-sandbox-platform-report-v1"
    state: SandboxPlatformState
    diagnostic_code: str = Field(min_length=1, max_length=80)
    executable_identity: str | None = Field(default=None, max_length=180)
    feature_state: SandboxFeatureState
    required_settings_supported: tuple[str, ...]
    protected_client_supported: bool
    report_identity: str = Field(
        pattern=r"^windows-sandbox-platform-report:v1:sha256:[0-9a-f]{64}$"
    )

    @model_validator(mode="after")
    def _validate_report(self) -> SandboxPlatformReport:
        if tuple(sorted(set(self.required_settings_supported))) != (
            self.required_settings_supported
        ):
            raise ValueError("platform setting support must be sorted and unique")
        if self.state is SandboxPlatformState.AVAILABLE and (
            self.executable_identity is None
            or self.feature_state is not SandboxFeatureState.ENABLED
            or set(self.required_settings_supported) != set(_REQUIRED_WSB_SETTINGS)
        ):
            raise ValueError("available platform lacks required WSB support")
        payload = self.model_dump(mode="json", exclude={"report_identity"})
        if self.report_identity != _identity(
            "windows-sandbox-platform-report:v1",
            payload,
        ):
            raise ValueError("platform report identity mismatch")
        return self

    @classmethod
    def available(cls, *, protected_client_supported: bool) -> SandboxPlatformReport:
        payload = {
            "contract_version": "windows-sandbox-platform-report-v1",
            "state": SandboxPlatformState.AVAILABLE,
            "diagnostic_code": "sandbox-executable-present",
            "executable_identity": _identity("windows-sandbox-executable", "available"),
            "feature_state": SandboxFeatureState.ENABLED,
            "required_settings_supported": tuple(sorted(_REQUIRED_WSB_SETTINGS)),
            "protected_client_supported": protected_client_supported,
        }
        json_payload = {
            key: value.value if isinstance(value, Enum) else value for key, value in payload.items()
        }
        return cls(
            **payload,
            report_identity=_identity(
                "windows-sandbox-platform-report:v1",
                json_payload,
            ),
        )

    @classmethod
    def unavailable(
        cls,
        *,
        state: SandboxPlatformState,
        diagnostic_code: str,
        feature_state: SandboxFeatureState = SandboxFeatureState.UNKNOWN,
    ) -> SandboxPlatformReport:
        if state is SandboxPlatformState.AVAILABLE:
            raise ValueError("unavailable report cannot use available state")
        payload = {
            "contract_version": "windows-sandbox-platform-report-v1",
            "state": state,
            "diagnostic_code": diagnostic_code,
            "executable_identity": None,
            "feature_state": feature_state,
            "required_settings_supported": (),
            "protected_client_supported": False,
        }
        json_payload = {
            key: value.value if isinstance(value, Enum) else value for key, value in payload.items()
        }
        return cls(
            **payload,
            report_identity=_identity(
                "windows-sandbox-platform-report:v1",
                json_payload,
            ),
        )


class SandboxProcessTopologyEntry(BaseModel):
    """One closed process-image/PID observation from the Sandbox topology."""

    model_config = _CLOSED_MODEL_CONFIG

    image_name: Literal[
        "WindowsSandbox.exe",
        "WindowsSandboxClient.exe",
        "WindowsSandboxRemoteSession.exe",
        "WindowsSandboxServer.exe",
        "VmmemWindowsSandbox",
    ]
    process_id: int = Field(ge=1, le=(2**31) - 1)


@dataclass(frozen=True, slots=True)
class SandboxProcessResult:
    returncode: int
    timed_out: bool
    before_processes: tuple[SandboxProcessTopologyEntry, ...] = ()
    launched_processes: tuple[SandboxProcessTopologyEntry, ...] = ()
    after_processes: tuple[SandboxProcessTopologyEntry, ...] = ()
    termination_confirmed: bool = True

    @property
    def launched_process_ids(self) -> tuple[int, ...]:
        return tuple(item.process_id for item in self.launched_processes)


class SandboxPlatform(Protocol):
    def probe(self) -> SandboxPlatformReport: ...

    def launch(
        self,
        config_path: Path,
        *,
        timeout_seconds: int,
    ) -> SandboxProcessResult: ...


class WindowsSandboxPlatform:
    """Narrow process/platform seam; tests inject a fake implementation."""

    def __init__(
        self,
        *,
        host_is_windows: bool | None = None,
        executable_override: Path | None = None,
        windows_build: int | None = None,
        feature_state_override: SandboxFeatureState | None = None,
    ) -> None:
        self._executable: Path | None = None
        self._host_is_windows = os.name == "nt" if host_is_windows is None else host_is_windows
        self._executable_override = executable_override
        self._windows_build = windows_build
        self._feature_state_override = feature_state_override

    @staticmethod
    def _probe_feature_state() -> SandboxFeatureState:
        system_root = Path(os.environ.get("SYSTEMROOT", r"C:\Windows"))
        powershell = system_root / "System32" / "WindowsPowerShell" / "v1.0" / "powershell.exe"
        if not powershell.is_file():
            discovered = shutil.which("powershell.exe")
            if discovered is None:
                return SandboxFeatureState.UNKNOWN
            powershell = Path(discovered)
        command = (
            "$feature=Get-CimInstance -ClassName Win32_OptionalFeature "
            "-Filter \"Name='Containers-DisposableClientVM'\" "
            "-ErrorAction Stop; if ($null -eq $feature) { 'missing' } "
            "else { [string]$feature.InstallState }"
        )
        creation_flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
        try:
            completed = subprocess.run(
                [
                    str(powershell),
                    "-NoLogo",
                    "-NoProfile",
                    "-NonInteractive",
                    "-Command",
                    command,
                ],
                check=False,
                capture_output=True,
                shell=False,
                timeout=15,
                creationflags=creation_flags,
            )
        except (OSError, subprocess.TimeoutExpired):
            return SandboxFeatureState.UNKNOWN
        if completed.returncode != 0:
            return SandboxFeatureState.UNKNOWN
        state = completed.stdout.decode("ascii", errors="ignore").strip()
        if state == "1":
            return SandboxFeatureState.ENABLED
        if state in {"2", "3"}:
            return SandboxFeatureState.DISABLED
        return SandboxFeatureState.UNKNOWN

    def probe(self) -> SandboxPlatformReport:
        if not self._host_is_windows:
            return SandboxPlatformReport.unavailable(
                state=SandboxPlatformState.NEEDS_INFO,
                diagnostic_code="windows-host-required",
            )
        discovered = (
            str(self._executable_override)
            if self._executable_override is not None
            else shutil.which("WindowsSandbox.exe")
        )
        if discovered is None:
            system_root = Path(os.environ.get("SYSTEMROOT", r"C:\Windows"))
            candidate = system_root / "System32" / "WindowsSandbox.exe"
            discovered = str(candidate) if candidate.is_file() else None
        if discovered is None:
            return SandboxPlatformReport.unavailable(
                state=SandboxPlatformState.NEEDS_INFO,
                diagnostic_code="sandbox-executable-unavailable",
            )
        try:
            executable = Path(discovered).resolve(strict=True)
            if not executable.is_file():
                raise OSError("sandbox executable is not a regular file")
        except (OSError, RuntimeError):
            return SandboxPlatformReport.unavailable(
                state=SandboxPlatformState.NEEDS_INFO,
                diagnostic_code="sandbox-executable-unreadable",
            )
        feature_state = self._feature_state_override or self._probe_feature_state()
        if feature_state is SandboxFeatureState.UNKNOWN:
            return SandboxPlatformReport.unavailable(
                state=SandboxPlatformState.NEEDS_INFO,
                diagnostic_code="sandbox-feature-state-unavailable",
                feature_state=feature_state,
            )
        if feature_state is SandboxFeatureState.DISABLED:
            return SandboxPlatformReport.unavailable(
                state=SandboxPlatformState.NOT_QUALIFIED,
                diagnostic_code="sandbox-feature-disabled",
                feature_state=feature_state,
            )
        if self._windows_build is None:
            try:
                build = int(sys.getwindowsversion().build)
            except (AttributeError, ValueError):
                build = 0
        else:
            build = self._windows_build
        if build <= 0:
            return SandboxPlatformReport.unavailable(
                state=SandboxPlatformState.NEEDS_INFO,
                diagnostic_code="windows-build-unavailable",
                feature_state=feature_state,
            )
        if build < 22000:
            return SandboxPlatformReport.unavailable(
                state=SandboxPlatformState.NOT_QUALIFIED,
                diagnostic_code="required-wsb-settings-unsupported",
                feature_state=feature_state,
            )
        report = SandboxPlatformReport.available(
            protected_client_supported=build >= 22000,
        )
        payload = report.model_dump(mode="json", exclude={"report_identity"})
        try:
            executable_bytes = executable.read_bytes()
            executable_size = executable.stat().st_size
        except OSError:
            return SandboxPlatformReport.unavailable(
                state=SandboxPlatformState.NEEDS_INFO,
                diagnostic_code="sandbox-executable-unreadable",
                feature_state=feature_state,
            )
        payload["executable_identity"] = _identity(
            "windows-sandbox-executable",
            {
                "sha256": sha256(executable_bytes).hexdigest(),
                "byte_length": executable_size,
            },
        )
        self._executable = executable
        return SandboxPlatformReport(
            **payload,
            report_identity=_identity(
                "windows-sandbox-platform-report:v1",
                payload,
            ),
        )

    def launch(
        self,
        config_path: Path,
        *,
        timeout_seconds: int,
    ) -> SandboxProcessResult:
        if self._executable is None:
            report = self.probe()
            if report.state is not SandboxPlatformState.AVAILABLE:
                return SandboxProcessResult(returncode=-1, timed_out=False)
        assert self._executable is not None
        before_processes = self._sandbox_process_topology()
        if before_processes is None:
            return SandboxProcessResult(
                returncode=-1,
                timed_out=False,
                termination_confirmed=False,
            )
        if before_processes:
            return SandboxProcessResult(
                returncode=-1,
                timed_out=False,
                before_processes=before_processes,
                termination_confirmed=False,
            )
        try:
            process = subprocess.Popen(
                [str(self._executable), str(config_path)],
                shell=False,
            )
        except OSError:
            return SandboxProcessResult(returncode=-1, timed_out=False)
        deadline = time.monotonic() + timeout_seconds
        launcher = SandboxProcessTopologyEntry(
            image_name="WindowsSandbox.exe",
            process_id=process.pid,
        )
        observed: dict[int, SandboxProcessTopologyEntry] = {
            launcher.process_id: launcher
        }
        launcher_returncode: int | None = None
        startup_deadline = min(deadline, time.monotonic() + 30)
        while time.monotonic() < deadline:
            if launcher_returncode is None:
                launcher_returncode = process.poll()
            current = self._sandbox_process_topology()
            if current is None:
                termination_confirmed, after_processes = self._terminate_and_confirm(
                    process,
                    before_processes=before_processes,
                    observed=observed,
                )
                return SandboxProcessResult(
                    returncode=-1,
                    timed_out=False,
                    before_processes=before_processes,
                    launched_processes=self._ordered_topology(observed.values()),
                    after_processes=after_processes,
                    termination_confirmed=termination_confirmed,
                )
            before_ids = {item.process_id for item in before_processes}
            current_by_id = {item.process_id: item for item in current}
            concurrent_launcher = any(
                item.image_name == "WindowsSandbox.exe"
                and item.process_id != process.pid
                and item.process_id not in before_ids
                for item in current
            )
            for item in current:
                if item.process_id not in before_ids:
                    observed[item.process_id] = item
            active = set(observed).intersection(current_by_id)
            if concurrent_launcher:
                termination_confirmed, after_processes = self._terminate_and_confirm(
                    process,
                    before_processes=before_processes,
                    observed=observed,
                )
                return SandboxProcessResult(
                    returncode=-1,
                    timed_out=False,
                    before_processes=before_processes,
                    launched_processes=self._ordered_topology(observed.values()),
                    after_processes=after_processes,
                    termination_confirmed=termination_confirmed,
                )
            if launcher_returncode not in {None, 0}:
                termination_confirmed, after_processes = self._terminate_and_confirm(
                    process,
                    before_processes=before_processes,
                    observed=observed,
                )
                return SandboxProcessResult(
                    returncode=launcher_returncode,
                    timed_out=False,
                    before_processes=before_processes,
                    launched_processes=self._ordered_topology(observed.values()),
                    after_processes=after_processes,
                    termination_confirmed=termination_confirmed,
                )
            if launcher_returncode == 0 and len(observed) > 1 and not active:
                return SandboxProcessResult(
                    returncode=0,
                    timed_out=False,
                    before_processes=before_processes,
                    launched_processes=self._ordered_topology(observed.values()),
                    after_processes=current,
                )
            if (
                launcher_returncode == 0
                and len(observed) == 1
                and time.monotonic() >= startup_deadline
            ):
                termination_confirmed, after_processes = self._terminate_and_confirm(
                    process,
                    before_processes=before_processes,
                    observed=observed,
                )
                return SandboxProcessResult(
                    returncode=-1,
                    timed_out=False,
                    before_processes=before_processes,
                    launched_processes=self._ordered_topology(observed.values()),
                    after_processes=after_processes,
                    termination_confirmed=termination_confirmed,
                )
            time.sleep(0.25)

        termination_confirmed, after_processes = self._terminate_and_confirm(
            process,
            before_processes=before_processes,
            observed=observed,
        )
        return SandboxProcessResult(
            returncode=-1,
            timed_out=True,
            before_processes=before_processes,
            launched_processes=self._ordered_topology(observed.values()),
            after_processes=after_processes,
            termination_confirmed=termination_confirmed,
        )

    @staticmethod
    def _ordered_topology(
        processes: Iterable[SandboxProcessTopologyEntry],
    ) -> tuple[SandboxProcessTopologyEntry, ...]:
        return tuple(
            sorted(
                processes,
                key=lambda item: (item.image_name.casefold(), item.process_id),
            )
        )

    @staticmethod
    def _sandbox_process_topology() -> tuple[SandboxProcessTopologyEntry, ...] | None:
        system_root = Path(os.environ.get("SYSTEMROOT", r"C:\Windows"))
        tasklist = system_root / "System32" / "tasklist.exe"
        creation_flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
        try:
            completed = subprocess.run(
                [str(tasklist), "/FO", "CSV", "/NH"],
                check=False,
                capture_output=True,
                shell=False,
                timeout=10,
                creationflags=creation_flags,
            )
        except (OSError, subprocess.TimeoutExpired):
            return None
        if completed.returncode != 0 or len(completed.stdout) > 8 * 1024 * 1024:
            return None
        names = {
            "windowssandbox.exe": "WindowsSandbox.exe",
            "windowssandboxclient.exe": "WindowsSandboxClient.exe",
            "windowssandboxremotesession.exe": "WindowsSandboxRemoteSession.exe",
            "windowssandboxserver.exe": "WindowsSandboxServer.exe",
            "vmmemwindowssandbox": "VmmemWindowsSandbox",
        }
        text = completed.stdout.decode("utf-8", errors="replace")
        result: dict[int, SandboxProcessTopologyEntry] = {}
        for row in csv.reader(text.splitlines()):
            if not row or row[0].casefold() not in names:
                continue
            if len(row) < 2:
                return None
            try:
                process_id = int(row[1])
                item = SandboxProcessTopologyEntry(
                    image_name=names[row[0].casefold()],
                    process_id=process_id,
                )
            except (TypeError, ValueError):
                return None
            existing = result.get(process_id)
            if existing is not None and existing != item:
                return None
            result[process_id] = item
        return WindowsSandboxPlatform._ordered_topology(result.values())

    def _terminate_and_confirm(
        self,
        process: subprocess.Popen[bytes],
        *,
        before_processes: tuple[SandboxProcessTopologyEntry, ...],
        observed: dict[int, SandboxProcessTopologyEntry],
    ) -> tuple[bool, tuple[SandboxProcessTopologyEntry, ...]]:
        self._terminate_process_tree(process.pid)
        try:
            process.wait(timeout=15)
        except subprocess.TimeoutExpired:
            process.kill()
        deadline = time.monotonic() + 30
        last_current: tuple[SandboxProcessTopologyEntry, ...] = ()
        before_ids = {item.process_id for item in before_processes}
        while time.monotonic() < deadline:
            current = self._sandbox_process_topology()
            if current is None:
                time.sleep(0.25)
                continue
            last_current = current
            current_by_id = {item.process_id: item for item in current}
            active = {
                process_id: item
                for process_id, item in current_by_id.items()
                if process_id not in before_ids
            }
            observed.update(active)
            if process.poll() is not None and not active:
                return True, current
            time.sleep(0.25)
        return False, last_current

    @staticmethod
    def _terminate_process_tree(process_id: int) -> None:
        creation_flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
        try:
            subprocess.run(
                ["taskkill.exe", "/PID", str(process_id), "/T", "/F"],
                check=False,
                capture_output=True,
                shell=False,
                timeout=15,
                creationflags=creation_flags,
            )
        except (OSError, subprocess.TimeoutExpired):
            return


def validate_wsb_configuration(
    xml: str,
    *,
    mapping_manifest: ResolvedSandboxMappingManifest,
    protected_client_supported: bool,
) -> None:
    try:
        root = ElementTree.fromstring(xml)
    except ElementTree.ParseError as exc:
        raise SandboxContractError(SandboxFailureReason.MAPPING_CONTRACT_INVALID) from exc
    if root.tag != "Configuration":
        raise SandboxContractError(SandboxFailureReason.MAPPING_CONTRACT_INVALID)
    if root.findtext("Networking") != "Disable":
        raise SandboxContractError(SandboxFailureReason.NETWORKING_ENABLED)
    expected_disabled = {
        "vGPU",
        "AudioInput",
        "VideoInput",
        "PrinterRedirection",
        "ClipboardRedirection",
    }
    if any(root.findtext(name) != "Disable" for name in expected_disabled):
        raise SandboxContractError(SandboxFailureReason.PLATFORM_CONTRACT_INVALID)
    protected_client = root.find("ProtectedClient")
    if protected_client_supported:
        if protected_client is None or protected_client.text != "Enable":
            raise SandboxContractError(SandboxFailureReason.PLATFORM_CONTRACT_INVALID)
    elif protected_client is not None:
        raise SandboxContractError(SandboxFailureReason.PLATFORM_CONTRACT_INVALID)
    folders = root.findall("./MappedFolders/MappedFolder")
    if len(folders) != len(mapping_manifest.bindings):
        raise SandboxContractError(SandboxFailureReason.MAPPING_CONTRACT_INVALID)
    for folder, binding in zip(folders, mapping_manifest.bindings, strict=True):
        if folder.findtext("HostFolder") != str(binding.host_path):
            raise SandboxContractError(SandboxFailureReason.MAPPING_CONTRACT_INVALID)
        if _windows_normalized(folder.findtext("SandboxFolder") or "") != (
            _windows_normalized(binding.guest_path)
        ):
            raise SandboxContractError(SandboxFailureReason.MAPPING_CONTRACT_INVALID)
        read_only = (folder.findtext("ReadOnly") or "").casefold() == "true"
        if read_only != binding.read_only:
            reason = (
                SandboxFailureReason.WRITABLE_INPUT_MAPPING
                if binding.role is not SandboxMappingRole.RESULTS_EXPORT
                else SandboxFailureReason.MAPPING_CONTRACT_INVALID
            )
            raise SandboxContractError(reason)
    command = root.findtext("./LogonCommand/Command") or ""
    if "C:\\QCF17\\Input\\Runner\\bootstrap.ps1" not in command:
        raise SandboxContractError(SandboxFailureReason.PLATFORM_CONTRACT_INVALID)


def _input_tree_identity(
    root: Path,
    *,
    namespace: str,
    content_validation: Literal[
        "scan", "sealed_reviewed_source", "sealed_offline_bundle"
    ] = "scan",
) -> str:
    if content_validation not in {
        "scan",
        "sealed_reviewed_source",
        "sealed_offline_bundle",
    }:
        raise SandboxContractError(SandboxFailureReason.FORBIDDEN_INPUT)
    resolved = root.resolve(strict=True)
    rows: list[dict[str, object]] = []
    total = 0
    for path in sorted(resolved.rglob("*"), key=lambda item: item.relative_to(resolved).as_posix()):
        relative_path = path.relative_to(resolved).as_posix()
        metadata = path.lstat()
        if stat.S_ISLNK(metadata.st_mode) or bool(
            getattr(metadata, "st_file_attributes", 0) & _WINDOWS_REPARSE_POINT_ATTRIBUTE
        ):
            raise SandboxContractError(SandboxFailureReason.FORBIDDEN_INPUT)
        if path.is_dir():
            continue
        if not path.is_file() or metadata.st_size > 64 * 1024 * 1024:
            raise SandboxContractError(SandboxFailureReason.FORBIDDEN_INPUT)
        content = path.read_bytes()
        total += len(content)
        if total > 512 * 1024 * 1024:
            raise SandboxContractError(SandboxFailureReason.FORBIDDEN_INPUT)
        if content_validation == "scan":
            _validate_input_content(relative_path, content)
        rows.append(
            {
                "relative_path": relative_path,
                "byte_length": len(content),
                "sha256": sha256(content).hexdigest(),
            }
        )
    if not rows:
        raise SandboxContractError(SandboxFailureReason.FORBIDDEN_INPUT)
    return _identity(
        namespace,
        {
            "content_validation": content_validation,
            "entries": rows,
        },
    )


def _resolve_required_directory(
    path: Path,
    *,
    reason: SandboxFailureReason,
) -> Path:
    try:
        expanded = path.expanduser()
        metadata = expanded.lstat()
        if (
            not stat.S_ISDIR(metadata.st_mode)
            or stat.S_ISLNK(metadata.st_mode)
            or bool(getattr(metadata, "st_file_attributes", 0) & _WINDOWS_REPARSE_POINT_ATTRIBUTE)
        ):
            raise SandboxContractError(reason)
        return expanded.resolve(strict=True)
    except SandboxContractError:
        raise
    except (OSError, RuntimeError) as exc:
        raise SandboxContractError(reason) from exc


def _stage_runner_input(
    repository_root: Path,
    source: StagedSourceInput,
    destination: Path,
) -> str:
    expected = {
        "scripts/windows_sandbox/bootstrap.ps1": "bootstrap.ps1",
        "scripts/windows_sandbox/verify_source.py": "verify_source.py",
    }
    by_path = {entry.relative_path: entry for entry in source.manifest.entries}
    if not set(expected).issubset(by_path):
        raise SandboxContractError(SandboxFailureReason.SOURCE_IDENTITY_MISMATCH)
    destination.mkdir(exist_ok=False)
    for source_path, target_name in expected.items():
        entry = by_path[source_path]
        content = _run_git(
            repository_root,
            ("cat-file", "blob", entry.git_object_id),
        ).stdout
        if sha256(content).hexdigest() != entry.sha256:
            raise SandboxContractError(SandboxFailureReason.SOURCE_IDENTITY_MISMATCH)
        _validate_input_content(target_name, content)
        (destination / target_name).write_bytes(content)
    return _input_tree_identity(destination, namespace="windows-sandbox-runner-input:v1")


@dataclass(frozen=True, slots=True)
class RunnerConstructionRequest:
    repository_root: Path
    runs_root: Path
    run_id: str
    execution_mode: SandboxExecutionMode
    protected_root_bindings: tuple[ProtectedRootBinding, ...]
    user_profile: Path
    host_temp: Path
    host_venv: Path
    runtime_bundle_root: Path | None = None


@dataclass(frozen=True, slots=True)
class SandboxRunPaths:
    run_root: Path
    source_input: Path
    runner_input: Path
    control_input: Path
    runtime_input: Path
    results_export: Path
    config_path: Path


@dataclass(frozen=True, slots=True)
class SandboxRunContract:
    qualification_outcome: SandboxQualificationOutcome
    platform_report: SandboxPlatformReport
    paths: SandboxRunPaths
    source: StagedSourceInput
    protected_root_manifest: ResolvedProtectedRootManifest
    guest_layout: GuestLayout
    environment_policy: EnvironmentPolicy
    runtime_bundle: RuntimeBundleInputContract
    mapping_manifest: ResolvedSandboxMappingManifest
    wsb_configuration: WsbConfiguration
    runner_identity: str
    guest_workspace_identity: str
    result_template: SandboxGuestResult
    config_path: Path
    forbidden_host_roots: tuple[Path, ...]
    no_launch_output_identity: str


class WindowsSandboxRunnerService:
    """The sole reviewed host owner for ISO-01 staging and launch validation."""

    def __init__(self, *, platform: SandboxPlatform | None = None) -> None:
        self._platform = platform or WindowsSandboxPlatform()

    @staticmethod
    def _qualification_for(state: SandboxPlatformState) -> SandboxQualificationOutcome:
        return (
            SandboxQualificationOutcome.NEEDS_INFO
            if state is SandboxPlatformState.NEEDS_INFO
            else SandboxQualificationOutcome.NOT_QUALIFIED
        )

    def validate_only(self, request: RunnerConstructionRequest) -> SandboxRunContract:
        if not isinstance(request.execution_mode, SandboxExecutionMode):
            raise SandboxContractError(SandboxFailureReason.MAPPING_CONTRACT_INVALID)
        try:
            platform_report = self._platform.probe()
        except (OSError, RuntimeError) as exc:
            raise SandboxContractError(
                SandboxFailureReason.PLATFORM_UNAVAILABLE,
                qualification_outcome=SandboxQualificationOutcome.NEEDS_INFO,
            ) from exc
        if platform_report.state is not SandboxPlatformState.AVAILABLE:
            raise SandboxContractError(
                SandboxFailureReason.PLATFORM_UNAVAILABLE,
                qualification_outcome=self._qualification_for(platform_report.state),
            )
        protected = build_protected_root_manifest(request.protected_root_bindings)
        repository = _resolve_required_directory(
            request.repository_root,
            reason=SandboxFailureReason.SOURCE_REPOSITORY_INVALID,
        )
        runs_root = _resolve_required_directory(
            request.runs_root,
            reason=SandboxFailureReason.MAPPING_CONTRACT_INVALID,
        )
        if not re.fullmatch(r"[a-z0-9][a-z0-9._-]{7,63}", request.run_id):
            raise SandboxContractError(SandboxFailureReason.MAPPING_CONTRACT_INVALID)
        forbidden_roots = (
            repository,
            _resolve_required_directory(
                request.user_profile,
                reason=SandboxFailureReason.MAPPING_CONTRACT_INVALID,
            ),
            _resolve_required_directory(
                request.host_temp,
                reason=SandboxFailureReason.MAPPING_CONTRACT_INVALID,
            ),
            _resolve_required_directory(
                request.host_venv,
                reason=SandboxFailureReason.MAPPING_CONTRACT_INVALID,
            ),
        )
        if any(_paths_overlap(runs_root, root) for root in forbidden_roots):
            raise SandboxContractError(SandboxFailureReason.FORBIDDEN_HOST_MAPPING)
        if any(_paths_overlap(runs_root, binding.path) for binding in protected.bindings):
            raise SandboxContractError(SandboxFailureReason.PROTECTED_HOST_MAPPING)

        run_root = runs_root / request.run_id
        try:
            run_root.mkdir(exist_ok=False)
            input_root = run_root / "input"
            input_root.mkdir()
            source_root = input_root / "source"
            runner_root = input_root / "runner"
            control_root = input_root / "control"
            runtime_root = input_root / "runtime"
            results_root = run_root / "results-export"
            control_root.mkdir()
            results_root.mkdir()
        except OSError as exc:
            raise SandboxContractError(SandboxFailureReason.RESULTS_EXPORT_NOT_FRESH) from exc

        source = stage_reviewed_source(repository, source_root)
        source_mapping_identity = _input_tree_identity(
            source_root,
            namespace="windows-sandbox-source-input-tree:v1",
            content_validation="sealed_reviewed_source",
        )
        runner_bootstrap_identity = _stage_runner_input(
            repository,
            source,
            runner_root,
        )
        layout = build_guest_layout()
        environment_policy = build_environment_policy(
            run_live_environment=source.identity.run_live_environment,
            layout=layout,
        )
        if request.runtime_bundle_root is None:
            runtime_root.mkdir()
            runtime_bundle = build_runtime_bundle_placeholder()
            runtime_contract_path = runtime_root / "runtime-bundle-contract.json"
            runtime_contract_path.write_bytes(
                _canonical_json_bytes(runtime_bundle.model_dump(mode="json")) + b"\n"
            )
            runtime_content_validation = "scan"
        else:
            runtime_root = _resolve_required_directory(
                request.runtime_bundle_root,
                reason=SandboxFailureReason.RUNTIME_BUNDLE_NOT_READY,
            )
            source_binding = OfflineBundleSourceBinding(
                contract_version=source.identity.contract_version,
                commit_id=source.identity.commit_id,
                tree_hash=source.identity.tree_hash,
                source_input_identity=source.identity.source_input_identity,
                source_manifest_identity=source.identity.source_manifest_identity,
                source_archive_sha256=source.identity.archive_sha256,
            )
            try:
                bundle_manifest = verify_offline_runtime_bundle(
                    runtime_root,
                    expected_source=source_binding,
                )
                manifest_bytes = (runtime_root / BUNDLE_MANIFEST_FILENAME).read_bytes()
            except (OSError, ValueError) as exc:
                raise SandboxContractError(
                    SandboxFailureReason.RUNTIME_BUNDLE_NOT_READY
                ) from exc
            runtime_bundle = build_sealed_runtime_bundle_input(
                bundle_manifest,
                manifest_sha256=sha256(manifest_bytes).hexdigest(),
            )
            runtime_content_validation = "sealed_offline_bundle"
        runtime_mapping_identity = _input_tree_identity(
            runtime_root,
            namespace="windows-sandbox-runtime-input:v1",
            content_validation=runtime_content_validation,
        )

        preliminary_bindings = (
            SandboxMappingBinding(
                SandboxMappingRole.SOURCE,
                source_root,
                _PINNED_GUEST_MAPPING_PATHS[SandboxMappingRole.SOURCE],
                True,
                source_mapping_identity,
            ),
            SandboxMappingBinding(
                SandboxMappingRole.RUNNER,
                runner_root,
                _PINNED_GUEST_MAPPING_PATHS[SandboxMappingRole.RUNNER],
                True,
                runner_bootstrap_identity,
            ),
            SandboxMappingBinding(
                SandboxMappingRole.CONTROL,
                control_root,
                _PINNED_GUEST_MAPPING_PATHS[SandboxMappingRole.CONTROL],
                True,
                _identity("windows-sandbox-control-input:v1", "pending"),
            ),
            SandboxMappingBinding(
                SandboxMappingRole.RUNTIME_BUNDLE,
                runtime_root,
                _PINNED_GUEST_MAPPING_PATHS[SandboxMappingRole.RUNTIME_BUNDLE],
                True,
                runtime_mapping_identity,
            ),
            SandboxMappingBinding(
                SandboxMappingRole.RESULTS_EXPORT,
                results_root,
                _PINNED_GUEST_MAPPING_PATHS[SandboxMappingRole.RESULTS_EXPORT],
                False,
                _identity("windows-sandbox-empty-results:v1", request.run_id),
                fresh_empty=True,
            ),
        )
        preliminary_manifest = build_mapping_manifest(
            preliminary_bindings,
            protected_manifest=protected,
            forbidden_host_roots=forbidden_roots,
        )
        configuration = render_wsb_configuration(
            preliminary_manifest,
            protected_client_supported=platform_report.protected_client_supported,
        )
        guest_workspace_identity = _identity(
            "windows-sandbox-guest-workspace:v1",
            {
                "source_input_identity": source.identity.source_input_identity,
                "layout_identity": layout.layout_identity,
                "environment_policy_identity": environment_policy.policy_identity,
                "execution_mode": request.execution_mode.value,
            },
        )
        control_payload = {
            "contract_version": "windows-sandbox-path-redacted-control-v1",
            "execution_mode": request.execution_mode.value,
            "platform_report": platform_report.model_dump(mode="json"),
            "protected_roots": protected.safe_projection(),
            "source": source.identity.model_dump(mode="json"),
            "runtime_bundle": runtime_bundle.model_dump(mode="json"),
            "runner_bootstrap_identity": runner_bootstrap_identity,
            "wsb_config_identity": configuration.config_identity,
            "guest_workspace_identity": guest_workspace_identity,
            "guest_layout": {
                **layout.model_dump(mode="json"),
                "writable_paths": list(layout.writable_paths()),
            },
            "environment": environment_policy.model_dump(mode="json"),
            "wsb_policy": configuration.policy.model_dump(mode="json"),
            "result_schema_identity": RESULT_SCHEMA_IDENTITY,
            "completion_marker_contract_identity": COMPLETION_MARKER_CONTRACT_IDENTITY,
        }
        (control_root / "run-control.json").write_bytes(
            _canonical_json_bytes(control_payload) + b"\n"
        )
        control_identity = _input_tree_identity(
            control_root,
            namespace="windows-sandbox-control-input:v1",
        )
        final_bindings = tuple(
            replace(binding, content_identity=control_identity)
            if binding.role is SandboxMappingRole.CONTROL
            else binding
            for binding in preliminary_bindings
        )
        mapping_manifest = build_mapping_manifest(
            final_bindings,
            protected_manifest=protected,
            forbidden_host_roots=forbidden_roots,
        )
        final_configuration = render_wsb_configuration(
            mapping_manifest,
            protected_client_supported=platform_report.protected_client_supported,
        )
        if final_configuration.config_identity != configuration.config_identity:
            raise SandboxContractError(SandboxFailureReason.PLATFORM_CONTRACT_INVALID)
        validate_wsb_configuration(
            final_configuration.xml,
            mapping_manifest=mapping_manifest,
            protected_client_supported=platform_report.protected_client_supported,
        )
        config_path = run_root / "sandbox.wsb"
        config_path.write_text(final_configuration.xml, encoding="utf-8", newline="\n")
        material = RunnerIdentityMaterial(
            protected_root_manifest_identity=protected.manifest_identity,
            source_input_identity=source.identity.source_input_identity,
            wsb_config_identity=final_configuration.config_identity,
            mapping_manifest_identity=mapping_manifest.mapping_manifest_identity,
            guest_layout_identity=layout.layout_identity,
            environment_policy_identity=environment_policy.policy_identity,
            result_schema_identity=RESULT_SCHEMA_IDENTITY,
            runtime_bundle_contract_identity=(runtime_bundle.runtime_bundle_contract_identity),
            runner_bootstrap_identity=runner_bootstrap_identity,
            control_inventory_identity=control_identity,
        )
        runner_identity = build_runner_identity(material)
        result_template = build_guest_result_template(
            execution_mode=request.execution_mode,
            runner_identity=runner_identity,
            wsb_config_identity=final_configuration.config_identity,
            source_commit_id=source.identity.commit_id,
            source_tree_hash=source.identity.tree_hash,
            source_archive_sha256=source.identity.archive_sha256,
            source_manifest_identity=source.identity.source_manifest_identity,
            protected_root_manifest_identity=protected.manifest_identity,
            runtime_bundle_contract_identity=(runtime_bundle.runtime_bundle_contract_identity),
            mapping_manifest_identity=mapping_manifest.mapping_manifest_identity,
            guest_workspace_identity=guest_workspace_identity,
        )
        no_launch_payload = {
            "contract_version": "windows-sandbox-no-launch-validation-v1",
            "qualification_outcome": (SandboxQualificationOutcome.CONSTRUCTION_VALIDATED.value),
            "runner_identity": runner_identity,
            "result_template_identity": result_template.result_identity,
            "platform_report_identity": platform_report.report_identity,
        }
        paths = SandboxRunPaths(
            run_root=run_root,
            source_input=source_root,
            runner_input=runner_root,
            control_input=control_root,
            runtime_input=runtime_root,
            results_export=results_root,
            config_path=config_path,
        )
        return SandboxRunContract(
            qualification_outcome=SandboxQualificationOutcome.CONSTRUCTION_VALIDATED,
            platform_report=platform_report,
            paths=paths,
            source=source,
            protected_root_manifest=protected,
            guest_layout=layout,
            environment_policy=environment_policy,
            runtime_bundle=runtime_bundle,
            mapping_manifest=mapping_manifest,
            wsb_configuration=final_configuration,
            runner_identity=runner_identity,
            guest_workspace_identity=guest_workspace_identity,
            result_template=result_template,
            config_path=config_path,
            forbidden_host_roots=forbidden_roots,
            no_launch_output_identity=_identity(
                "windows-sandbox-no-launch-validation:v1",
                no_launch_payload,
            ),
        )

    def _revalidate_before_launch(self, contract: SandboxRunContract) -> None:
        current_platform = self._platform.probe()
        if (
            current_platform.state is not SandboxPlatformState.AVAILABLE
            or current_platform != contract.platform_report
        ):
            raise SandboxContractError(SandboxFailureReason.PLATFORM_CONTRACT_INVALID)

        protected = build_protected_root_manifest(contract.protected_root_manifest.bindings)
        if protected.contract != contract.protected_root_manifest.contract:
            raise SandboxContractError(SandboxFailureReason.PROTECTED_ROOT_MANIFEST_INCOMPLETE)
        mapping_manifest = build_mapping_manifest(
            contract.mapping_manifest.bindings,
            protected_manifest=protected,
            forbidden_host_roots=contract.forbidden_host_roots,
        )
        if mapping_manifest.contract != contract.mapping_manifest.contract:
            raise SandboxContractError(SandboxFailureReason.MAPPING_CONTRACT_INVALID)

        namespaces = {
            SandboxMappingRole.SOURCE: "windows-sandbox-source-input-tree:v1",
            SandboxMappingRole.RUNNER: "windows-sandbox-runner-input:v1",
            SandboxMappingRole.CONTROL: "windows-sandbox-control-input:v1",
            SandboxMappingRole.RUNTIME_BUNDLE: "windows-sandbox-runtime-input:v1",
        }
        by_role = {binding.role: binding for binding in mapping_manifest.bindings}
        for role, namespace in namespaces.items():
            actual_identity = _input_tree_identity(
                by_role[role].host_path,
                namespace=namespace,
                content_validation=(
                    "sealed_reviewed_source"
                    if role is SandboxMappingRole.SOURCE
                    else "sealed_offline_bundle"
                    if role is SandboxMappingRole.RUNTIME_BUNDLE
                    and contract.runtime_bundle.state == "sealed"
                    else "scan"
                ),
            )
            if actual_identity != by_role[role].content_identity:
                reason = (
                    SandboxFailureReason.SOURCE_IDENTITY_MISMATCH
                    if role is SandboxMappingRole.SOURCE
                    else SandboxFailureReason.MAPPING_CONTRACT_INVALID
                )
                raise SandboxContractError(reason)

        if contract.runtime_bundle.state == "sealed":
            source_binding = OfflineBundleSourceBinding(
                contract_version=contract.source.identity.contract_version,
                commit_id=contract.source.identity.commit_id,
                tree_hash=contract.source.identity.tree_hash,
                source_input_identity=contract.source.identity.source_input_identity,
                source_manifest_identity=contract.source.identity.source_manifest_identity,
                source_archive_sha256=contract.source.identity.archive_sha256,
            )
            try:
                runtime_manifest = verify_offline_runtime_bundle(
                    by_role[SandboxMappingRole.RUNTIME_BUNDLE].host_path,
                    expected_source=source_binding,
                )
                manifest_bytes = (
                    by_role[SandboxMappingRole.RUNTIME_BUNDLE].host_path
                    / BUNDLE_MANIFEST_FILENAME
                ).read_bytes()
                expected_runtime_contract = build_sealed_runtime_bundle_input(
                    runtime_manifest,
                    manifest_sha256=sha256(manifest_bytes).hexdigest(),
                )
            except (OSError, ValueError) as exc:
                raise SandboxContractError(
                    SandboxFailureReason.RUNTIME_BUNDLE_NOT_READY
                ) from exc
            if expected_runtime_contract != contract.runtime_bundle:
                raise SandboxContractError(SandboxFailureReason.RUNTIME_BUNDLE_NOT_READY)

        expected_layout = build_guest_layout()
        if contract.guest_layout != expected_layout:
            raise SandboxContractError(SandboxFailureReason.GUEST_LAYOUT_INVALID)
        expected_environment = build_environment_policy(
            run_live_environment=contract.source.identity.run_live_environment,
            layout=expected_layout,
        )
        if contract.environment_policy != expected_environment:
            raise SandboxContractError(SandboxFailureReason.ENVIRONMENT_POLICY_INVALID)

        expected_configuration = render_wsb_configuration(
            mapping_manifest,
            protected_client_supported=current_platform.protected_client_supported,
        )
        if expected_configuration != contract.wsb_configuration:
            raise SandboxContractError(SandboxFailureReason.PLATFORM_CONTRACT_INVALID)
        validate_wsb_configuration(
            expected_configuration.xml,
            mapping_manifest=mapping_manifest,
            protected_client_supported=current_platform.protected_client_supported,
        )
        try:
            config_metadata = contract.config_path.lstat()
            config_bytes = contract.config_path.read_bytes()
        except OSError as exc:
            raise SandboxContractError(SandboxFailureReason.PLATFORM_CONTRACT_INVALID) from exc
        if (
            not stat.S_ISREG(config_metadata.st_mode)
            or stat.S_ISLNK(config_metadata.st_mode)
            or config_bytes != expected_configuration.xml.encode("utf-8")
        ):
            raise SandboxContractError(SandboxFailureReason.PLATFORM_CONTRACT_INVALID)

        expected_workspace_identity = _identity(
            "windows-sandbox-guest-workspace:v1",
            {
                "source_input_identity": contract.source.identity.source_input_identity,
                "layout_identity": contract.guest_layout.layout_identity,
                "environment_policy_identity": contract.environment_policy.policy_identity,
                "execution_mode": contract.result_template.execution_mode.value,
            },
        )
        if expected_workspace_identity != contract.guest_workspace_identity:
            raise SandboxContractError(SandboxFailureReason.RESULT_IDENTITY_MISMATCH)
        material = RunnerIdentityMaterial(
            protected_root_manifest_identity=protected.manifest_identity,
            source_input_identity=contract.source.identity.source_input_identity,
            wsb_config_identity=expected_configuration.config_identity,
            mapping_manifest_identity=mapping_manifest.mapping_manifest_identity,
            guest_layout_identity=contract.guest_layout.layout_identity,
            environment_policy_identity=contract.environment_policy.policy_identity,
            result_schema_identity=RESULT_SCHEMA_IDENTITY,
            runtime_bundle_contract_identity=(
                contract.runtime_bundle.runtime_bundle_contract_identity
            ),
            runner_bootstrap_identity=by_role[SandboxMappingRole.RUNNER].content_identity,
            control_inventory_identity=by_role[SandboxMappingRole.CONTROL].content_identity,
        )
        if build_runner_identity(material) != contract.runner_identity:
            raise SandboxContractError(SandboxFailureReason.RESULT_IDENTITY_MISMATCH)
        expected_result = build_guest_result_template(
            execution_mode=contract.result_template.execution_mode,
            runner_identity=contract.runner_identity,
            wsb_config_identity=expected_configuration.config_identity,
            source_commit_id=contract.source.identity.commit_id,
            source_tree_hash=contract.source.identity.tree_hash,
            source_archive_sha256=contract.source.identity.archive_sha256,
            source_manifest_identity=contract.source.identity.source_manifest_identity,
            protected_root_manifest_identity=protected.manifest_identity,
            runtime_bundle_contract_identity=(
                contract.runtime_bundle.runtime_bundle_contract_identity
            ),
            mapping_manifest_identity=mapping_manifest.mapping_manifest_identity,
            guest_workspace_identity=expected_workspace_identity,
        )
        if expected_result != contract.result_template:
            raise SandboxContractError(SandboxFailureReason.RESULT_IDENTITY_MISMATCH)

    def launch(
        self,
        contract: SandboxRunContract,
        *,
        timeout_seconds: int,
    ) -> ValidatedResultExport:
        self._revalidate_before_launch(contract)
        if contract.runtime_bundle.state != "sealed":
            raise SandboxContractError(SandboxFailureReason.RUNTIME_BUNDLE_NOT_READY)
        process = self._platform.launch(
            contract.config_path,
            timeout_seconds=timeout_seconds,
        )
        if process.timed_out or process.returncode != 0:
            raise SandboxContractError(SandboxFailureReason.SANDBOX_LAUNCH_FAILED)
        return validate_result_export(
            contract.paths.results_export,
            expected=contract.result_template,
        )

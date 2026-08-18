"""Trusted-host and bounded-result contracts for ISO-03 qualification."""

from __future__ import annotations

import json
import os
import re
import shutil
import stat
import subprocess
from base64 import b64encode
from collections.abc import Callable
from dataclasses import dataclass
from enum import Enum
from hashlib import sha256
from pathlib import Path, PurePosixPath, PureWindowsPath
from typing import Literal
from xml.sax.saxutils import escape as xml_escape

from pydantic import BaseModel, ConfigDict, Field, model_validator

import tradingagents.offline_runtime_bundle as offline_runtime_bundle_module
import tradingagents.windows_sandbox_acceptance as windows_sandbox_acceptance_module
import tradingagents.windows_sandbox_runner as windows_sandbox_runner_module
from tradingagents.offline_runtime_bundle import (
    BUNDLE_MANIFEST_FILENAME,
    OfflineBundleContractError,
    OfflineBundleSourceBinding,
    verify_offline_runtime_bundle,
)
from tradingagents.windows_sandbox_acceptance import (
    QUALIFIED_RUNNER_SOURCE_FILES,
    QUALIFIED_RUNNER_TARGET_FILES,
    TICKET06_ACCEPTANCE_RESULT_SCHEMA_IDENTITY,
    AcceptanceCommandStatus,
    QualifiedRunnerFile,
    QualifiedRunnerManifest,
    Ticket06AcceptanceControlBase,
    Ticket06AcceptanceControlEnvelope,
    Ticket06AcceptanceStructuralQualification,
    Ticket06ControlEnvironment,
    Ticket06ControlGuestLayout,
    Ticket06ControlRuntime,
    Ticket06ControlSource,
    ValidatedTicket06AcceptanceExport,
    build_ticket06_acceptance_control_envelope,
    build_ticket06_command_ledger,
    build_ticket06_control_environment,
    content_identity as acceptance_content_identity,
    qualify_ticket06_acceptance_runner_structure,
    validate_recorded_iso03_report_json,
    validate_ticket06_acceptance_export,
)
from tradingagents.windows_sandbox_runner import (
    EnvironmentPolicy,
    GuestLayout,
    ProtectedObjectKind,
    ProtectedRootManifest,
    ProtectedRootRole,
    ResolvedProtectedRootManifest,
    ResolvedSandboxMappingManifest,
    ReviewedSourceIdentity,
    RunnerIdentityMaterial,
    RuntimeBundleInputContract,
    SandboxContractError,
    SandboxExecutionMode,
    SandboxFailureReason,
    SandboxMappingBinding,
    SandboxMappingRole,
    SandboxPlatform,
    SandboxPlatformReport,
    SandboxPlatformState,
    SandboxProcessResult,
    SandboxProcessTopologyEntry,
    StagedSourceInput,
    WindowsSandboxPlatform,
    WindowsSandboxRunnerService,
    WsbConfiguration,
    build_environment_policy,
    build_guest_layout,
    build_mapping_manifest,
    build_protected_root_manifest,
    build_runner_identity,
    build_sealed_runtime_bundle_input,
    default_protected_root_bindings,
    render_wsb_configuration,
    stage_reviewed_source,
    validate_wsb_configuration,
)

PROTECTED_HOST_INVENTORY_VERSION = "windows-sandbox-protected-host-inventory-v1"
HOST_INVENTORY_DELTA_VERSION = "windows-sandbox-host-inventory-delta-v1"
ISO04_SANDBOX_PROCESS_EVIDENCE_FILENAME = "iso04-sandbox-process-evidence.json"
_ISO04_SANDBOX_PROCESS_EVIDENCE_MAXIMUM_BYTES = 65_536
ISO04_TICKET06_OPERATIONAL_EVIDENCE_FILENAME = (
    "iso04-ticket06-operational-evidence.json"
)
_ISO04_TICKET06_OPERATIONAL_EVIDENCE_MAXIMUM_BYTES = 131_072
ISO04_TICKET06_REPORT_FILENAME = "iso04-ticket06-acceptance-report.json"
_CLOSED_MODEL_CONFIG = ConfigDict(frozen=True, extra="forbid")
_WINDOWS_REPARSE_POINT_ATTRIBUTE = 0x400
_UNSAFE_EXPORT_PATTERN = re.compile(
    rb"(?:-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----|"
    rb"\bAKIA[0-9A-Z]{16}\b|\bgh[oprsu]_[A-Za-z0-9]{20,}\b|"
    rb"\bsk-[A-Za-z0-9_-]{20,}\b|https?://[^\s/:@]+:[^\s/@]+@)",
    flags=re.IGNORECASE,
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


BOUNDARY_QUALIFICATION_RESULT_SCHEMA_IDENTITY = _identity(
    "windows-sandbox-boundary-result-schema:v1",
    {
        "contract_version": "windows-sandbox-boundary-qualification-result-v1",
        "execution_mode": "boundary_qualification",
        "probe_mechanisms": [
            "normal_python",
            "python_i",
            "python_s",
            "python_e",
            "multiprocessing_spawn",
            "powershell",
            "cmd",
            "direct_filesystem",
            "sqlite",
        ],
        "completion_written_last": True,
        "acceptance_runner_structural_qualification_bound": True,
        "maximum_export_bytes": 524_288,
        "host_inventory_authority": False,
    },
)


def _path_identity(path: Path) -> str:
    normalized = os.path.normcase(str(path)).replace("/", "\\").casefold()
    return _identity("host-path", normalized)


def host_path_identity(path: str | Path) -> str:
    """Return the path-redacted identity used by host and guest contracts."""

    return _path_identity(Path(path))


def _is_reparse(metadata: os.stat_result) -> bool:
    return stat.S_ISLNK(metadata.st_mode) or bool(
        getattr(metadata, "st_file_attributes", 0) & _WINDOWS_REPARSE_POINT_ATTRIBUTE
    )


def _hash_stable_file(path: Path) -> tuple[int, str]:
    try:
        link_before = path.lstat()
    except OSError as exc:
        raise SandboxContractError(SandboxFailureReason.PROTECTED_ROOT_MISSING) from exc
    if _is_reparse(link_before):
        try:
            target_before = os.readlink(path)
            link_after = path.lstat()
            target_after = os.readlink(path)
        except OSError as exc:
            raise SandboxContractError(
                SandboxFailureReason.PROTECTED_ROOT_REPARSE_POINT
            ) from exc
        if (
            not _is_reparse(link_after)
            or link_before.st_mtime_ns != link_after.st_mtime_ns
            or target_before != target_after
        ):
            raise SandboxContractError(SandboxFailureReason.PROTECTED_ROOT_REPARSE_POINT)
        descriptor = _canonical_json_bytes(
            {
                "contract_version": "protected-internal-reparse-descriptor-v1",
                "target": target_after,
            }
        )
        return len(descriptor), sha256(descriptor).hexdigest()
    digest = sha256()
    try:
        with path.open("rb") as handle:
            opened_before = os.fstat(handle.fileno())
            while chunk := handle.read(1024 * 1024):
                digest.update(chunk)
            opened_after = os.fstat(handle.fileno())
        path_after = path.stat(follow_symlinks=False)
    except OSError as exc:
        raise SandboxContractError(SandboxFailureReason.PROTECTED_ROOT_MISSING) from exc
    opened_before_key = (
        opened_before.st_dev,
        opened_before.st_ino,
        opened_before.st_size,
        opened_before.st_mtime_ns,
    )
    opened_after_key = (
        opened_after.st_dev,
        opened_after.st_ino,
        opened_after.st_size,
        opened_after.st_mtime_ns,
    )
    if (
        opened_before_key != opened_after_key
        or path_after.st_size != opened_after.st_size
        or path_after.st_mtime_ns != opened_after.st_mtime_ns
        or _is_reparse(path_after)
    ):
        raise SandboxContractError(SandboxFailureReason.PROTECTED_ROOT_REPARSE_POINT)
    return opened_after.st_size, digest.hexdigest()


class ProtectedHostFile(BaseModel):
    model_config = _CLOSED_MODEL_CONFIG

    canonical_path_identity: str = Field(pattern=r"^host-path:sha256:[0-9a-f]{64}$")
    byte_length: int = Field(ge=0)
    sha256: str = Field(pattern=r"^[0-9a-f]{64}$")


class ProtectedRootFile(BaseModel):
    model_config = _CLOSED_MODEL_CONFIG

    relative_path: str = Field(min_length=1, max_length=2048)
    canonical_path_identity: str = Field(pattern=r"^host-path:sha256:[0-9a-f]{64}$")
    byte_length: int = Field(ge=0)
    sha256: str = Field(pattern=r"^[0-9a-f]{64}$")


class ProtectedRootInventory(BaseModel):
    model_config = _CLOSED_MODEL_CONFIG

    role: ProtectedRootRole
    object_kind: ProtectedObjectKind
    root_path_identity: str = Field(pattern=r"^host-path:sha256:[0-9a-f]{64}$")
    file_count: int = Field(ge=0)
    byte_length: int = Field(ge=0)
    files: tuple[ProtectedRootFile, ...]
    inventory_identity: str = Field(
        pattern=r"^windows-sandbox-protected-root-inventory:v1:sha256:[0-9a-f]{64}$"
    )

    @model_validator(mode="after")
    def _validate_inventory(self) -> ProtectedRootInventory:
        if self.file_count != len(self.files) or self.byte_length != sum(
            item.byte_length for item in self.files
        ):
            raise ValueError("protected root inventory counts are inconsistent")
        if tuple(item.relative_path for item in self.files) != tuple(
            sorted(item.relative_path for item in self.files)
        ):
            raise ValueError("protected root inventory paths are not canonical")
        payload = self.model_dump(mode="json", exclude={"inventory_identity"})
        if self.inventory_identity != _identity(
            "windows-sandbox-protected-root-inventory:v1", payload
        ):
            raise ValueError("protected root inventory identity mismatch")
        return self


class ProtectedHostInventory(BaseModel):
    model_config = _CLOSED_MODEL_CONFIG

    contract_version: str = PROTECTED_HOST_INVENTORY_VERSION
    protected_root_manifest_identity: str
    immutable_verifier_identity: str
    roots: tuple[ProtectedRootInventory, ...]
    files: tuple[ProtectedHostFile, ...]
    file_count: int = Field(ge=0)
    byte_length: int = Field(ge=0)
    semantic_inventory: dict[str, object]
    semantic_inventory_identity: str
    inventory_identity: str = Field(
        pattern=(
            r"^windows-sandbox-protected-host-inventory:v1:sha256:[0-9a-f]{64}$"
        )
    )

    @model_validator(mode="after")
    def _validate_inventory(self) -> ProtectedHostInventory:
        if tuple(root.role for root in self.roots) != tuple(ProtectedRootRole):
            raise ValueError("protected host inventory roles are incomplete")
        if self.file_count != len(self.files) or self.byte_length != sum(
            item.byte_length for item in self.files
        ):
            raise ValueError("protected host inventory counts are inconsistent")
        if self.semantic_inventory_identity != _identity(
            "ticket06-immutable-semantic-inventory:v1", self.semantic_inventory
        ):
            raise ValueError("semantic inventory identity mismatch")
        payload = self.model_dump(mode="json", exclude={"inventory_identity"})
        if self.inventory_identity != _identity(
            "windows-sandbox-protected-host-inventory:v1", payload
        ):
            raise ValueError("protected host inventory identity mismatch")
        return self


class HostInventoryDelta(BaseModel):
    model_config = _CLOSED_MODEL_CONFIG

    contract_version: str = HOST_INVENTORY_DELTA_VERSION
    before_inventory_identity: str
    after_inventory_identity: str
    manifest_equal: bool
    immutable_verifier_equal: bool
    semantic_equal: bool
    appended: int = Field(ge=0)
    deleted: int = Field(ge=0)
    rewritten: int = Field(ge=0)
    changed_roles: tuple[ProtectedRootRole, ...]
    equal: bool
    delta_identity: str

    @model_validator(mode="after")
    def _validate_delta(self) -> HostInventoryDelta:
        expected_equal = all(
            (
                self.manifest_equal,
                self.immutable_verifier_equal,
                self.semantic_equal,
                self.appended == 0,
                self.deleted == 0,
                self.rewritten == 0,
                not self.changed_roles,
            )
        )
        if self.equal != expected_equal:
            raise ValueError("host inventory delta equality is inconsistent")
        payload = self.model_dump(mode="json", exclude={"delta_identity"})
        if self.delta_identity != _identity(
            "windows-sandbox-host-inventory-delta:v1", payload
        ):
            raise ValueError("host inventory delta identity mismatch")
        return self


class ProtectedWriterQuiescenceContract(BaseModel):
    """Explicit operator-owned exclusion window around trusted host inventories."""

    model_config = _CLOSED_MODEL_CONFIG

    contract_version: Literal["windows-sandbox-protected-writer-quiescence-v1"] = (
        "windows-sandbox-protected-writer-quiescence-v1"
    )
    operator_attested: Literal[True] = True
    scope: Literal["sentinel-through-host-after"] = "sentinel-through-host-after"
    protected_writers_permitted: Literal[False] = False
    contract_identity: str = Field(
        pattern=(
            r"^windows-sandbox-protected-writer-quiescence:v1:sha256:"
            r"[0-9a-f]{64}$"
        )
    )

    @model_validator(mode="after")
    def _validate_contract(self) -> ProtectedWriterQuiescenceContract:
        payload = self.model_dump(mode="json", exclude={"contract_identity"})
        if self.contract_identity != _identity(
            "windows-sandbox-protected-writer-quiescence:v1", payload
        ):
            raise ValueError("protected-writer quiescence identity mismatch")
        return self


class ProtectedWriterQuiescenceEvidence(BaseModel):
    """Evidence that construction did not alter state before the launch inventory."""

    model_config = _CLOSED_MODEL_CONFIG

    contract: ProtectedWriterQuiescenceContract
    sentinel_inventory_identity: str = Field(
        pattern=(
            r"^windows-sandbox-protected-host-inventory:v1:sha256:"
            r"[0-9a-f]{64}$"
        )
    )
    immediate_before_inventory_identity: str = Field(
        pattern=(
            r"^windows-sandbox-protected-host-inventory:v1:sha256:"
            r"[0-9a-f]{64}$"
        )
    )
    prelaunch_delta: HostInventoryDelta
    evidence_identity: str = Field(
        pattern=(
            r"^windows-sandbox-protected-writer-quiescence-evidence:v1:sha256:"
            r"[0-9a-f]{64}$"
        )
    )

    @model_validator(mode="after")
    def _validate_evidence(self) -> ProtectedWriterQuiescenceEvidence:
        if (
            not self.prelaunch_delta.equal
            or self.prelaunch_delta.before_inventory_identity
            != self.sentinel_inventory_identity
            or self.prelaunch_delta.after_inventory_identity
            != self.immediate_before_inventory_identity
            or self.sentinel_inventory_identity
            != self.immediate_before_inventory_identity
        ):
            raise ValueError("protected-writer quiescence prelaunch gate failed")
        payload = self.model_dump(mode="json", exclude={"evidence_identity"})
        if self.evidence_identity != _identity(
            "windows-sandbox-protected-writer-quiescence-evidence:v1", payload
        ):
            raise ValueError("protected-writer quiescence evidence identity mismatch")
        return self


class WindowsHostQualificationState(str, Enum):
    QUALIFIED = "qualified"
    NEEDS_INFO = "needs-info"
    NOT_QUALIFIED = "not-qualified"


class WindowsHostPlatformFacts(BaseModel):
    model_config = _CLOSED_MODEL_CONFIG

    edition_id: str = Field(min_length=1, max_length=80)
    product_name: str = Field(min_length=1, max_length=160)
    build_number: int = Field(ge=1)
    architecture: str = Field(min_length=1, max_length=32)
    hypervisor_present: bool
    virtualization_firmware_enabled: bool
    logical_processor_count: int = Field(ge=1)
    physical_memory_bytes: int = Field(ge=1)
    system_drive_free_bytes: int = Field(ge=0)
    host_is_virtual_machine: bool
    nested_virtualization_ready: bool
    systeminfo_identity: str = Field(min_length=1, max_length=180)


HostCommandRunner = Callable[
    [tuple[str, ...], int], subprocess.CompletedProcess[bytes]
]


def _run_host_command(
    command: tuple[str, ...], timeout_seconds: int
) -> subprocess.CompletedProcess[bytes]:
    creation_flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    return subprocess.run(
        command,
        check=False,
        capture_output=True,
        shell=False,
        timeout=timeout_seconds,
        creationflags=creation_flags,
    )


def capture_windows_host_platform_facts(
    *,
    command_runner: HostCommandRunner = _run_host_command,
    powershell_path: Path | None = None,
    systeminfo_path: Path | None = None,
) -> WindowsHostPlatformFacts:
    """Capture bounded host facts without inferring readiness from one flag."""

    system_root = Path(os.environ.get("SYSTEMROOT", r"C:\Windows"))
    powershell = powershell_path or (
        system_root / "System32" / "WindowsPowerShell" / "v1.0" / "powershell.exe"
    )
    if not powershell.is_file():
        discovered = shutil.which("powershell.exe")
        if discovered is None:
            raise SandboxContractError(SandboxFailureReason.PLATFORM_UNAVAILABLE)
        powershell = Path(discovered)
    systeminfo = systeminfo_path or system_root / "System32" / "systeminfo.exe"
    if not systeminfo.is_file():
        discovered = shutil.which("systeminfo.exe")
        if discovered is None:
            raise SandboxContractError(SandboxFailureReason.PLATFORM_UNAVAILABLE)
        systeminfo = Path(discovered)

    script = (
        "$ErrorActionPreference='Stop';"
        "$os=Get-ItemProperty -LiteralPath "
        "'HKLM:\\SOFTWARE\\Microsoft\\Windows NT\\CurrentVersion';"
        "$cs=Get-CimInstance -ClassName Win32_ComputerSystem -ErrorAction Stop;"
        "$cpu=Get-CimInstance -ClassName Win32_Processor -ErrorAction Stop | "
        "Select-Object -First 1;"
        "$drive=Get-CimInstance -ClassName Win32_LogicalDisk "
        "-Filter (\"DeviceID='\"+$env:SystemDrive+\"'\") -ErrorAction Stop;"
        "$isVm=($cs.Model -match '(?i)(virtual|vmware|kvm|hyper-v|virtualbox)');"
        "[pscustomobject]@{"
        "edition_id=[string]$os.EditionID;"
        "product_name=[string]$os.ProductName;"
        "build_number=[int]$os.CurrentBuildNumber;"
        "architecture=[string]$env:PROCESSOR_ARCHITECTURE;"
        "hypervisor_present=[bool]$cs.HypervisorPresent;"
        "virtualization_firmware_enabled=[bool]$cpu.VirtualizationFirmwareEnabled;"
        "logical_processor_count=[int]$cs.NumberOfLogicalProcessors;"
        "physical_memory_bytes=[int64]$cs.TotalPhysicalMemory;"
        "system_drive_free_bytes=[int64]$drive.FreeSpace;"
        "host_is_virtual_machine=[bool]$isVm;"
        "nested_virtualization_ready=[bool]((-not $isVm) -or "
        "$cpu.VirtualizationFirmwareEnabled)"
        "} | ConvertTo-Json -Compress"
    )
    try:
        facts_result = command_runner(
            (
                str(powershell),
                "-NoLogo",
                "-NoProfile",
                "-NonInteractive",
                "-Command",
                script,
            ),
            30,
        )
        systeminfo_result = command_runner((str(systeminfo),), 30)
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise SandboxContractError(SandboxFailureReason.PLATFORM_UNAVAILABLE) from exc
    if (
        facts_result.returncode != 0
        or systeminfo_result.returncode != 0
        or len(facts_result.stdout) > 32_768
        or len(systeminfo_result.stdout) > 512_000
    ):
        raise SandboxContractError(SandboxFailureReason.PLATFORM_UNAVAILABLE)
    try:
        payload = json.loads(facts_result.stdout.decode("utf-8-sig"))
    except (UnicodeDecodeError, json.JSONDecodeError, TypeError) as exc:
        raise SandboxContractError(SandboxFailureReason.PLATFORM_UNAVAILABLE) from exc
    if not isinstance(payload, dict):
        raise SandboxContractError(SandboxFailureReason.PLATFORM_UNAVAILABLE)
    payload["systeminfo_identity"] = _identity(
        "windows-systeminfo:v1",
        {
            "byte_length": len(systeminfo_result.stdout),
            "sha256": sha256(systeminfo_result.stdout).hexdigest(),
        },
    )
    try:
        return WindowsHostPlatformFacts.model_validate(payload)
    except ValueError as exc:
        raise SandboxContractError(SandboxFailureReason.PLATFORM_UNAVAILABLE) from exc


def _unavailable_windows_host_platform_facts() -> WindowsHostPlatformFacts:
    return WindowsHostPlatformFacts(
        edition_id="unknown",
        product_name="unknown",
        build_number=1,
        architecture="unknown",
        hypervisor_present=False,
        virtualization_firmware_enabled=False,
        logical_processor_count=1,
        physical_memory_bytes=1,
        system_drive_free_bytes=0,
        host_is_virtual_machine=False,
        nested_virtualization_ready=False,
        systeminfo_identity=_identity(
            "windows-systeminfo:v1",
            "host-platform-facts-unavailable",
        ),
    )


def run_minimal_sandbox_launch(
    *,
    platform: SandboxPlatform,
    evidence_root: Path,
    run_id: str,
    timeout_seconds: int,
) -> MinimalSandboxLaunchEvidence:
    """Boot a fresh Sandbox, export one fixed marker, and shut it down."""

    if re.fullmatch(r"[a-z0-9][a-z0-9._-]{7,63}", run_id) is None:
        raise SandboxContractError(SandboxFailureReason.MAPPING_CONTRACT_INVALID)
    try:
        resolved_evidence = evidence_root.resolve(strict=True)
        metadata = resolved_evidence.lstat()
        if not resolved_evidence.is_dir() or _is_reparse(metadata):
            raise OSError("minimal launch evidence root is unsafe")
        run_root = resolved_evidence / f"minimal-{run_id}"
        run_root.mkdir(exist_ok=False)
        export_root = run_root / "results-export"
        export_root.mkdir()
    except OSError as exc:
        raise SandboxContractError(SandboxFailureReason.RESULTS_EXPORT_NOT_FRESH) from exc

    marker_payload = {
        "contract_version": "windows-sandbox-minimal-launch-marker-v1",
        "run_id": run_id,
        "status": "pass",
    }
    marker_bytes = _canonical_json_bytes(marker_payload)
    marker_text = marker_bytes.decode("utf-8").replace("'", "''")
    guest_script = (
        "$ErrorActionPreference='Stop';"
        "$path='C:\\QCF17\\Minimal\\minimal-launch.json';"
        f"[IO.File]::WriteAllText($path,'{marker_text}',"
        "[Text.UTF8Encoding]::new($false));"
        "shutdown.exe /s /t 0 /f"
    )
    encoded = b64encode(guest_script.encode("utf-16-le")).decode("ascii")
    command = f"powershell.exe -NoLogo -NoProfile -EncodedCommand {encoded}"
    xml = "\n".join(
        (
            '<?xml version="1.0" encoding="utf-8"?>',
            "<Configuration>",
            "  <vGPU>Disable</vGPU>",
            "  <Networking>Disable</Networking>",
            "  <AudioInput>Disable</AudioInput>",
            "  <VideoInput>Disable</VideoInput>",
            "  <PrinterRedirection>Disable</PrinterRedirection>",
            "  <ClipboardRedirection>Disable</ClipboardRedirection>",
            "  <ProtectedClient>Enable</ProtectedClient>",
            "  <MappedFolders>",
            "    <MappedFolder>",
            f"      <HostFolder>{xml_escape(str(export_root))}</HostFolder>",
            "      <SandboxFolder>C:\\QCF17\\Minimal</SandboxFolder>",
            "      <ReadOnly>false</ReadOnly>",
            "    </MappedFolder>",
            "  </MappedFolders>",
            "  <LogonCommand>",
            f"    <Command>{xml_escape(command)}</Command>",
            "  </LogonCommand>",
            "</Configuration>",
            "",
        )
    )
    config_identity = _identity("windows-sandbox-minimal-config:v1", xml)
    config_path = run_root / "minimal.wsb"
    config_path.write_text(xml, encoding="utf-8", newline="\n")
    process = platform.launch(config_path, timeout_seconds=timeout_seconds)
    marker_path = export_root / "minimal-launch.json"
    marker_sha256: str | None = None
    try:
        exported = tuple(export_root.iterdir())
        if len(exported) == 1 and exported[0] == marker_path:
            marker_metadata = marker_path.lstat()
            actual = marker_path.read_bytes()
            if (
                stat.S_ISREG(marker_metadata.st_mode)
                and not _is_reparse(marker_metadata)
                and actual == marker_bytes
            ):
                marker_sha256 = sha256(actual).hexdigest()
    except OSError:
        marker_sha256 = None
    succeeded = (
        process.returncode == 0
        and not process.timed_out
        and process.termination_confirmed
        and marker_sha256 is not None
    )
    return MinimalSandboxLaunchEvidence.create(
        succeeded=succeeded,
        returncode=process.returncode,
        timed_out=process.timed_out,
        config_identity=config_identity,
        marker_sha256=marker_sha256,
        before_processes=process.before_processes,
        launched_processes=process.launched_processes,
        after_processes=process.after_processes,
        termination_confirmed=process.termination_confirmed,
    )


class MinimalSandboxLaunchEvidence(BaseModel):
    model_config = _CLOSED_MODEL_CONFIG

    contract_version: Literal["windows-sandbox-minimal-launch-evidence-v1"] = (
        "windows-sandbox-minimal-launch-evidence-v1"
    )
    succeeded: bool
    returncode: int = Field(ge=-1, le=65535)
    timed_out: bool
    before_processes: tuple[SandboxProcessTopologyEntry, ...] = ()
    launched_processes: tuple[SandboxProcessTopologyEntry, ...] = ()
    after_processes: tuple[SandboxProcessTopologyEntry, ...] = ()
    termination_confirmed: bool = True
    config_identity: str = Field(min_length=1, max_length=180)
    marker_sha256: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    evidence_identity: str

    @model_validator(mode="after")
    def _validate_evidence(self) -> MinimalSandboxLaunchEvidence:
        if self.succeeded != (
            self.returncode == 0
            and not self.timed_out
            and self.termination_confirmed
            and self.marker_sha256 is not None
        ):
            raise ValueError("minimal Sandbox launch evidence is inconsistent")
        for rows in (
            self.before_processes,
            self.launched_processes,
            self.after_processes,
        ):
            if rows != WindowsSandboxPlatform._ordered_topology(rows) or len(
                {item.process_id for item in rows}
            ) != len(rows):
                raise ValueError("minimal Sandbox process topology is invalid")
        if self.succeeded and (
            self.before_processes
            or len(self.launched_processes) < 2
            or not any(
                item.image_name == "WindowsSandbox.exe"
                for item in self.launched_processes
            )
            or self.after_processes
        ):
            raise ValueError("successful minimal launch lacks closed process topology")
        payload = self.model_dump(mode="json", exclude={"evidence_identity"})
        if self.evidence_identity != _identity(
            "windows-sandbox-minimal-launch-evidence:v1", payload
        ):
            raise ValueError("minimal Sandbox launch evidence identity mismatch")
        return self

    @property
    def launched_process_ids(self) -> tuple[int, ...]:
        return tuple(item.process_id for item in self.launched_processes)

    @classmethod
    def create(
        cls,
        *,
        succeeded: bool,
        returncode: int,
        timed_out: bool,
        config_identity: str,
        marker_sha256: str | None,
        before_processes: tuple[SandboxProcessTopologyEntry, ...] = (),
        launched_processes: tuple[SandboxProcessTopologyEntry, ...] = (),
        after_processes: tuple[SandboxProcessTopologyEntry, ...] = (),
        termination_confirmed: bool = True,
    ) -> MinimalSandboxLaunchEvidence:
        payload = {
            "contract_version": "windows-sandbox-minimal-launch-evidence-v1",
            "succeeded": succeeded,
            "returncode": returncode,
            "timed_out": timed_out,
            "before_processes": [item.model_dump(mode="json") for item in before_processes],
            "launched_processes": [
                item.model_dump(mode="json") for item in launched_processes
            ],
            "after_processes": [item.model_dump(mode="json") for item in after_processes],
            "termination_confirmed": termination_confirmed,
            "config_identity": config_identity,
            "marker_sha256": marker_sha256,
        }
        return cls(
            **payload,
            evidence_identity=_identity(
                "windows-sandbox-minimal-launch-evidence:v1", payload
            ),
        )


class WindowsHostPlatformQualification(BaseModel):
    model_config = _CLOSED_MODEL_CONFIG

    contract_version: Literal["windows-sandbox-host-platform-qualification-v1"] = (
        "windows-sandbox-host-platform-qualification-v1"
    )
    state: WindowsHostQualificationState
    platform_report_identity: str
    facts: WindowsHostPlatformFacts
    minimal_launch: MinimalSandboxLaunchEvidence
    supported_edition: bool
    supported_build: bool
    supported_architecture: bool
    feature_enabled: bool
    hypervisor_ready: bool
    firmware_virtualization_ready: bool
    nested_virtualization_ready: bool
    resource_minimums_ready: bool
    sandbox_executable_ready: bool
    protected_client_ready: bool
    minimal_launch_succeeded: bool
    qualification_identity: str = Field(
        pattern=(
            r"^windows-sandbox-host-platform-qualification:v1:sha256:[0-9a-f]{64}$"
        )
    )

    @model_validator(mode="after")
    def _validate_qualification(self) -> WindowsHostPlatformQualification:
        checks = (
            self.supported_edition,
            self.supported_build,
            self.supported_architecture,
            self.feature_enabled,
            self.hypervisor_ready,
            self.firmware_virtualization_ready,
            self.nested_virtualization_ready,
            self.resource_minimums_ready,
            self.sandbox_executable_ready,
            self.protected_client_ready,
            self.minimal_launch_succeeded,
        )
        if self.state is WindowsHostQualificationState.QUALIFIED and not all(checks):
            raise ValueError("qualified Windows host has an unmet requirement")
        payload = self.model_dump(mode="json", exclude={"qualification_identity"})
        if self.qualification_identity != _identity(
            "windows-sandbox-host-platform-qualification:v1", payload
        ):
            raise ValueError("Windows host qualification identity mismatch")
        return self


def qualify_windows_sandbox_host(
    *,
    platform_report: SandboxPlatformReport,
    facts: WindowsHostPlatformFacts,
    minimal_launch: MinimalSandboxLaunchEvidence,
) -> WindowsHostPlatformQualification:
    supported_editions = {
        "education",
        "educationn",
        "enterprise",
        "enterprisen",
        "professional",
        "professionaln",
        "professionalworkstation",
        "professionalworkstationn",
    }
    payload = {
        "contract_version": "windows-sandbox-host-platform-qualification-v1",
        "platform_report_identity": platform_report.report_identity,
        "facts": facts.model_dump(mode="json"),
        "minimal_launch": minimal_launch.model_dump(mode="json"),
        "supported_edition": facts.edition_id.casefold() in supported_editions,
        "supported_build": facts.build_number >= 22000,
        "supported_architecture": facts.architecture.casefold() in {"amd64", "arm64"},
        "feature_enabled": platform_report.feature_state.value == "enabled",
        "hypervisor_ready": facts.hypervisor_present,
        "firmware_virtualization_ready": facts.virtualization_firmware_enabled,
        "nested_virtualization_ready": (
            not facts.host_is_virtual_machine or facts.nested_virtualization_ready
        ),
        "resource_minimums_ready": (
            facts.logical_processor_count >= 2
            and facts.physical_memory_bytes >= 4 * 1024**3
            and facts.system_drive_free_bytes >= 1024**3
        ),
        "sandbox_executable_ready": (
            platform_report.state is SandboxPlatformState.AVAILABLE
            and platform_report.executable_identity is not None
        ),
        "protected_client_ready": platform_report.protected_client_supported,
        "minimal_launch_succeeded": minimal_launch.succeeded,
    }
    requirements = tuple(
        value for key, value in payload.items() if key.endswith(("_ready", "_enabled"))
    ) + (
        payload["supported_edition"],
        payload["supported_build"],
        payload["supported_architecture"],
        payload["minimal_launch_succeeded"],
    )
    if all(requirements):
        state = WindowsHostQualificationState.QUALIFIED
    elif platform_report.state is SandboxPlatformState.NEEDS_INFO or (
        not minimal_launch.succeeded and minimal_launch.timed_out
    ):
        state = WindowsHostQualificationState.NEEDS_INFO
    else:
        state = WindowsHostQualificationState.NOT_QUALIFIED
    payload["state"] = state
    json_payload = {
        key: value.value if isinstance(value, Enum) else value for key, value in payload.items()
    }
    return WindowsHostPlatformQualification(
        **payload,
        qualification_identity=_identity(
            "windows-sandbox-host-platform-qualification:v1", json_payload
        ),
    )


class ProbeMechanism(str, Enum):
    NORMAL_PYTHON = "normal_python"
    PYTHON_ISOLATED = "python_i"
    PYTHON_NO_SITE = "python_s"
    PYTHON_IGNORE_ENVIRONMENT = "python_e"
    MULTIPROCESSING_SPAWN = "multiprocessing_spawn"
    POWERSHELL = "powershell"
    CMD = "cmd"
    DIRECT_FILESYSTEM = "direct_filesystem"
    SQLITE = "sqlite"


class ProbeObservation(str, Enum):
    HOST_OBJECT_ABSENT = "host_object_absent"
    OS_WRITE_DENIED = "os_write_denied"
    GUEST_LOCAL_ONLY = "guest_local_only"


class LocalWriteTarget(str, Enum):
    TEMP = "temp"
    TRADINGAGENTS_RUNTIME = "tradingagents_runtime"
    DATA = "data"
    CACHE = "cache"
    MARKET_HISTORY = "market_history"
    CHECKPOINTS = "checkpoints"
    REPORTS = "reports"
    LOGS = "logs"
    PAYLOADS = "payloads"
    SQLITE = "sqlite"
    SOURCE_TREE = "source_tree"


class AdversarialProbeTarget(BaseModel):
    model_config = _CLOSED_MODEL_CONFIG

    mechanism: ProbeMechanism
    target_host_path: str = Field(min_length=3, max_length=1024)
    target_path_identity: str = Field(pattern=r"^host-path:sha256:[0-9a-f]{64}$")

    @model_validator(mode="after")
    def _validate_target(self) -> AdversarialProbeTarget:
        target = PureWindowsPath(self.target_host_path)
        if (
            not target.is_absolute()
            or not target.drive
            or ".." in target.parts
            or self.target_path_identity != host_path_identity(self.target_host_path)
        ):
            raise ValueError("adversarial target is not canonical")
        return self


class AdversarialProbeResult(BaseModel):
    model_config = _CLOSED_MODEL_CONFIG

    mechanism: ProbeMechanism
    target_host_path: str = Field(min_length=3, max_length=1024)
    target_path_identity: str = Field(pattern=r"^host-path:sha256:[0-9a-f]{64}$")
    observation: ProbeObservation
    contained: bool

    @model_validator(mode="after")
    def _validate_probe(self) -> AdversarialProbeResult:
        target = PureWindowsPath(self.target_host_path)
        if (
            not target.is_absolute()
            or not target.drive
            or ".." in target.parts
            or self.target_path_identity != host_path_identity(self.target_host_path)
        ):
            raise ValueError("adversarial target must be an absolute Windows path")
        if not self.contained:
            raise ValueError("guest qualification cannot pass an uncontained probe")
        return self


class LocalWriteResult(BaseModel):
    model_config = _CLOSED_MODEL_CONFIG

    target: LocalWriteTarget
    succeeded: bool


class MappingVerification(BaseModel):
    model_config = _CLOSED_MODEL_CONFIG

    networking_disabled: bool
    read_only_input_mappings: bool
    exactly_one_writable_results_mapping: bool
    forbidden_host_mappings_absent: bool
    guest_read_only_write_checks_passed: bool

    @property
    def passed(self) -> bool:
        return all(self.model_dump().values())


class NetworkVerification(BaseModel):
    model_config = _CLOSED_MODEL_CONFIG

    ipv4_default_route_present: bool
    ipv6_default_route_present: bool
    external_route_usable: bool

    @property
    def passed(self) -> bool:
        return not any(self.model_dump().values())


BoundaryRunnerFile = QualifiedRunnerFile
BoundaryRunnerManifest = QualifiedRunnerManifest


class ReviewedHostHarnessFile(BaseModel):
    model_config = _CLOSED_MODEL_CONFIG

    relative_path: str = Field(min_length=1, max_length=160)
    byte_length: int = Field(ge=1, le=4 * 1024 * 1024)
    sha256: str = Field(pattern=r"^[0-9a-f]{64}$")


class ReviewedHostHarnessManifest(BaseModel):
    model_config = _CLOSED_MODEL_CONFIG

    contract_version: Literal["iso03-reviewed-host-harness-v1"] = (
        "iso03-reviewed-host-harness-v1"
    )
    implementation_base_commit_id: str = Field(pattern=r"^[0-9a-f]{40}$")
    candidate_tree_hash: str = Field(pattern=r"^[0-9a-f]{40}$")
    source_mode: Literal["reviewed_index"] = "reviewed_index"
    files: tuple[ReviewedHostHarnessFile, ...]
    manifest_identity: str = Field(
        pattern=r"^iso03-reviewed-host-harness:v1:sha256:[0-9a-f]{64}$"
    )

    @model_validator(mode="after")
    def _validate_manifest(self) -> ReviewedHostHarnessManifest:
        expected = (
            "scripts/run_iso03_windows_sandbox_qualification.py",
            "tradingagents/offline_runtime_bundle.py",
            "tradingagents/windows_sandbox_acceptance.py",
            "tradingagents/windows_sandbox_qualification.py",
            "tradingagents/windows_sandbox_runner.py",
        )
        if tuple(item.relative_path for item in self.files) != expected:
            raise ValueError("committed host harness file set is incomplete")
        payload = self.model_dump(mode="json", exclude={"manifest_identity"})
        if self.manifest_identity != _identity(
            "iso03-reviewed-host-harness:v1", payload
        ):
            raise ValueError("reviewed host harness identity mismatch")
        return self


class QualificationControlBase(BaseModel):
    model_config = _CLOSED_MODEL_CONFIG

    contract_version: Literal["windows-sandbox-boundary-control-base-v1"] = (
        "windows-sandbox-boundary-control-base-v1"
    )
    execution_mode: Literal["boundary_qualification"] = "boundary_qualification"
    run_id: str = Field(pattern=r"^[a-z0-9][a-z0-9._-]{7,63}$")
    qualification_run_identity: str = Field(min_length=1, max_length=180)
    source: ReviewedSourceIdentity
    runtime_bundle: RuntimeBundleInputContract
    runtime_bundle_identity: str = Field(min_length=1, max_length=180)
    runner_input: BoundaryRunnerManifest
    host_harness: ReviewedHostHarnessManifest
    protected_roots: ProtectedRootManifest
    probe_targets: tuple[AdversarialProbeTarget, ...]
    platform_qualification: WindowsHostPlatformQualification
    immutable_verifier_identity: str = Field(min_length=1, max_length=180)
    host_before_inventory_identity: str = Field(min_length=1, max_length=180)
    protected_writer_quiescence_contract_identity: str = Field(
        pattern=(
            r"^windows-sandbox-protected-writer-quiescence:v1:sha256:"
            r"[0-9a-f]{64}$"
        )
    )
    guest_layout: GuestLayout
    environment: EnvironmentPolicy
    result_schema_identity: str = Field(min_length=1, max_length=180)
    networking_disabled: Literal[True] = True
    input_mappings_read_only: Literal[True] = True
    exactly_one_writable_results_mapping: Literal[True] = True
    credential_inputs_present: Literal[False] = False
    ticket06_permitted: Literal[False] = False
    ticket07_permitted: Literal[False] = False
    provider_model_io_permitted: Literal[False] = False
    calendar_refresh_permitted: Literal[False] = False
    qualified_v1_activation_permitted: Literal[False] = False

    @model_validator(mode="after")
    def _validate_base(self) -> QualificationControlBase:
        if tuple(item.mechanism for item in self.probe_targets) != tuple(ProbeMechanism):
            raise ValueError("boundary probe target plan is incomplete or unordered")
        if self.runtime_bundle.state != "sealed":
            raise ValueError("boundary qualification requires a sealed runtime bundle")
        if self.result_schema_identity != BOUNDARY_QUALIFICATION_RESULT_SCHEMA_IDENTITY:
            raise ValueError("boundary result schema identity mismatch")
        if self.platform_qualification.state is not WindowsHostQualificationState.QUALIFIED:
            raise ValueError("boundary control requires a qualified Windows host")
        return self


class QualificationStaticBindings(BaseModel):
    model_config = _CLOSED_MODEL_CONFIG

    execution_mode: Literal["boundary_qualification"] = "boundary_qualification"
    qualification_run_identity: str = Field(min_length=1, max_length=180)
    source_commit_id: str = Field(pattern=r"^[0-9a-f]{40,64}$")
    source_tree_hash: str = Field(pattern=r"^[0-9a-f]{40,64}$")
    source_input_identity: str = Field(min_length=1, max_length=180)
    source_manifest_identity: str = Field(min_length=1, max_length=180)
    source_archive_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    runtime_bundle_identity: str = Field(min_length=1, max_length=180)
    runtime_bundle_manifest_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    runtime_bundle_contract_identity: str = Field(min_length=1, max_length=180)
    runner_identity: str = Field(min_length=1, max_length=180)
    wsb_config_identity: str = Field(min_length=1, max_length=180)
    protected_root_manifest_identity: str = Field(min_length=1, max_length=180)
    mapping_manifest_identity: str = Field(min_length=1, max_length=180)
    guest_workspace_identity: str = Field(min_length=1, max_length=180)
    qualification_control_identity: str = Field(min_length=1, max_length=180)
    platform_qualification_identity: str = Field(min_length=1, max_length=180)
    immutable_verifier_identity: str = Field(min_length=1, max_length=180)
    host_before_inventory_identity: str = Field(min_length=1, max_length=180)
    acceptance_runner_structure: Ticket06AcceptanceStructuralQualification


class BoundaryQualificationControlEnvelope(BaseModel):
    model_config = _CLOSED_MODEL_CONFIG

    contract_version: Literal["windows-sandbox-boundary-control-envelope-v1"] = (
        "windows-sandbox-boundary-control-envelope-v1"
    )
    base: QualificationControlBase
    qualification_control_identity: str = Field(
        pattern=r"^windows-sandbox-qualification-control:v1:sha256:[0-9a-f]{64}$"
    )
    source: OfflineBundleSourceBinding
    runtime_bundle: RuntimeBundleInputContract
    derived: QualificationStaticBindings

    @model_validator(mode="after")
    def _validate_envelope(self) -> BoundaryQualificationControlEnvelope:
        if self.qualification_control_identity != _identity(
            "windows-sandbox-qualification-control:v1",
            self.base.model_dump(mode="json"),
        ):
            raise ValueError("boundary control identity mismatch")
        if (
            self.source != _runtime_source_binding(self.base.source)
            or self.runtime_bundle != self.base.runtime_bundle
        ):
            raise ValueError("offline bootstrap compatibility binding mismatch")
        source = self.base.source
        runtime = self.base.runtime_bundle
        expected = {
            "qualification_run_identity": self.base.qualification_run_identity,
            "source_commit_id": source.commit_id,
            "source_tree_hash": source.tree_hash,
            "source_input_identity": source.source_input_identity,
            "source_manifest_identity": source.source_manifest_identity,
            "source_archive_sha256": source.archive_sha256,
            "runtime_bundle_identity": self.base.runtime_bundle_identity,
            "runtime_bundle_manifest_sha256": runtime.bundle_manifest_sha256,
            "runtime_bundle_contract_identity": (
                runtime.runtime_bundle_contract_identity
            ),
            "protected_root_manifest_identity": (
                self.base.protected_roots.manifest_identity
            ),
            "qualification_control_identity": self.qualification_control_identity,
            "platform_qualification_identity": (
                self.base.platform_qualification.qualification_identity
            ),
            "immutable_verifier_identity": self.base.immutable_verifier_identity,
            "host_before_inventory_identity": self.base.host_before_inventory_identity,
        }
        derived = self.derived.model_dump(mode="json")
        if any(derived[key] != value for key, value in expected.items()):
            raise ValueError("derived boundary control binding mismatch")
        acceptance_environment = build_ticket06_control_environment(
            self.base.environment.variables
        )
        acceptance = self.derived.acceptance_runner_structure
        host_module = next(
            (
                item
                for item in self.base.host_harness.files
                if item.relative_path
                == "tradingagents/windows_sandbox_qualification.py"
            ),
            None,
        )
        if (
            host_module is None
            or acceptance.runner_input_identity
            != self.base.runner_input.runner_input_identity
            or acceptance.environment_policy_identity
            != acceptance_environment.policy_identity
            or acceptance.host_harness_manifest_identity
            != self.base.host_harness.manifest_identity
            or acceptance.host_acceptance_module_sha256 != host_module.sha256
            or acceptance.boundary_runner_identity != self.derived.runner_identity
            or acceptance.wsb_config_identity != self.derived.wsb_config_identity
            or acceptance.mapping_manifest_identity
            != self.derived.mapping_manifest_identity
        ):
            raise ValueError("acceptance-runner structural binding mismatch")
        return self


class GuestQualificationResult(QualificationStaticBindings):
    contract_version: Literal["windows-sandbox-boundary-qualification-result-v1"] = (
        "windows-sandbox-boundary-qualification-result-v1"
    )
    source_verified: Literal[True] = True
    runtime_bundle_verified: Literal[True] = True
    guest_workspace_verified: Literal[True] = True
    mapping: MappingVerification
    network: NetworkVerification
    live_environment: tuple[tuple[str, str], ...] = Field(min_length=4, max_length=128)
    credential_inputs_present: Literal[False] = False
    probes: tuple[AdversarialProbeResult, ...]
    local_writes: tuple[LocalWriteResult, ...]
    guest_completion_status: Literal["pass"] = "pass"
    ticket06_executed: Literal[False] = False
    ticket07_executed: Literal[False] = False
    provider_model_io: Literal[False] = False
    calendar_refresh_executed: Literal[False] = False
    qualified_v1_enabled: Literal[False] = False
    result_identity: str = Field(
        pattern=r"^windows-sandbox-boundary-qualification-result:v1:sha256:[0-9a-f]{64}$"
    )

    @model_validator(mode="after")
    def _validate_result(self) -> GuestQualificationResult:
        if tuple(item.mechanism for item in self.probes) != tuple(ProbeMechanism):
            raise ValueError("adversarial probe matrix is incomplete or unordered")
        if tuple(item.target for item in self.local_writes) != tuple(LocalWriteTarget):
            raise ValueError("sandbox-local write matrix is incomplete or unordered")
        if not all(item.succeeded for item in self.local_writes):
            raise ValueError("sandbox-local write control failed")
        if not self.mapping.passed or not self.network.passed:
            raise ValueError("mapping or network qualification failed")
        environment = dict(self.live_environment)
        if tuple(name for name, _value in self.live_environment) != tuple(
            sorted(environment)
        ) or len(environment) != len(self.live_environment):
            raise ValueError("guest environment evidence is not canonical")
        if environment.get("PYTHON_DOTENV_DISABLED") != "1" or any(
            value != "0"
            for name, value in self.live_environment
            if name.startswith("RUN_LIVE_")
        ):
            raise ValueError("guest live or dotenv controls are unsafe")
        for required in (
            "RUN_LIVE_DEEPSEEK_TESTS",
            "RUN_LIVE_PROVIDER_TESTS",
            "RUN_LIVE_TUSHARE_TESTS",
        ):
            if environment.get(required) != "0":
                raise ValueError("required guest live opt-in is missing")
        payload = self.model_dump(mode="json", exclude={"result_identity"})
        if self.result_identity != _identity(
            "windows-sandbox-boundary-qualification-result:v1", payload
        ):
            raise ValueError("boundary qualification result identity mismatch")
        return self

    def static_bindings(self) -> QualificationStaticBindings:
        return QualificationStaticBindings.model_validate(
            self.model_dump(
                mode="json",
                include=set(QualificationStaticBindings.model_fields),
            )
        )

    def canonical_json_bytes(self) -> bytes:
        return _canonical_json_bytes(self.model_dump(mode="json"))


class QualificationCompletionMarker(BaseModel):
    model_config = _CLOSED_MODEL_CONFIG

    contract_version: Literal["windows-sandbox-boundary-completion-v1"] = (
        "windows-sandbox-boundary-completion-v1"
    )
    result_identity: str
    result_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    write_order: Literal["last"] = "last"
    marker_identity: str

    @model_validator(mode="after")
    def _validate_marker(self) -> QualificationCompletionMarker:
        payload = self.model_dump(mode="json", exclude={"marker_identity"})
        if self.marker_identity != _identity(
            "windows-sandbox-boundary-completion:v1", payload
        ):
            raise ValueError("boundary completion marker identity mismatch")
        return self

    def canonical_json_bytes(self) -> bytes:
        return _canonical_json_bytes(self.model_dump(mode="json"))


@dataclass(frozen=True, slots=True)
class ValidatedBoundaryQualificationExport:
    result: GuestQualificationResult
    completion: QualificationCompletionMarker
    export_identity: str
    host_qcf17_authority: bool = False


def build_guest_qualification_result(
    bindings: QualificationStaticBindings,
    *,
    mapping: MappingVerification,
    network: NetworkVerification,
    live_environment: tuple[tuple[str, str], ...],
    probes: tuple[AdversarialProbeResult, ...],
    local_writes: tuple[LocalWriteResult, ...],
) -> GuestQualificationResult:
    payload = {
        **bindings.model_dump(mode="json"),
        "contract_version": "windows-sandbox-boundary-qualification-result-v1",
        "source_verified": True,
        "runtime_bundle_verified": True,
        "guest_workspace_verified": True,
        "mapping": mapping.model_dump(mode="json"),
        "network": network.model_dump(mode="json"),
        "live_environment": [list(item) for item in live_environment],
        "credential_inputs_present": False,
        "probes": [probe.model_dump(mode="json") for probe in probes],
        "local_writes": [item.model_dump(mode="json") for item in local_writes],
        "guest_completion_status": "pass",
        "ticket06_executed": False,
        "ticket07_executed": False,
        "provider_model_io": False,
        "calendar_refresh_executed": False,
        "qualified_v1_enabled": False,
    }
    return GuestQualificationResult(
        **payload,
        result_identity=_identity(
            "windows-sandbox-boundary-qualification-result:v1", payload
        ),
    )


def build_qualification_completion_marker(
    *,
    result: GuestQualificationResult,
    result_bytes: bytes,
) -> QualificationCompletionMarker:
    payload = {
        "contract_version": "windows-sandbox-boundary-completion-v1",
        "result_identity": result.result_identity,
        "result_sha256": sha256(result_bytes).hexdigest(),
        "write_order": "last",
    }
    return QualificationCompletionMarker(
        **payload,
        marker_identity=_identity(
            "windows-sandbox-boundary-completion:v1", payload
        ),
    )


def validate_boundary_qualification_export(
    export_root: Path,
    *,
    expected: QualificationStaticBindings,
    expected_probe_targets: tuple[AdversarialProbeTarget, ...] | None = None,
) -> ValidatedBoundaryQualificationExport:
    try:
        root = export_root.resolve(strict=True)
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
        if (
            not stat.S_ISREG(metadata.st_mode)
            or _is_reparse(metadata)
            or metadata.st_size > 262_144
        ):
            raise SandboxContractError(SandboxFailureReason.UNSAFE_RESULT_EXPORT)
        total += metadata.st_size
    if total > 524_288:
        raise SandboxContractError(SandboxFailureReason.UNSAFE_RESULT_EXPORT)
    result_path = root / "result.json"
    marker_path = root / "completion.json"
    try:
        result_bytes = result_path.read_bytes()
        marker_bytes = marker_path.read_bytes()
        if _UNSAFE_EXPORT_PATTERN.search(result_bytes + marker_bytes):
            raise SandboxContractError(SandboxFailureReason.UNSAFE_RESULT_EXPORT)
        result = GuestQualificationResult.model_validate_json(result_bytes)
        marker = QualificationCompletionMarker.model_validate_json(marker_bytes)
    except SandboxContractError:
        raise
    except (OSError, ValueError, TypeError) as exc:
        raise SandboxContractError(SandboxFailureReason.COMPLETION_MARKER_INVALID) from exc
    if result.static_bindings() != expected:
        raise SandboxContractError(SandboxFailureReason.RESULT_IDENTITY_MISMATCH)
    if expected_probe_targets is not None and tuple(
        (item.mechanism, item.target_host_path, item.target_path_identity)
        for item in result.probes
    ) != tuple(
        (item.mechanism, item.target_host_path, item.target_path_identity)
        for item in expected_probe_targets
    ):
        raise SandboxContractError(SandboxFailureReason.RESULT_IDENTITY_MISMATCH)
    if (
        marker.result_identity != result.result_identity
        or marker.result_sha256 != sha256(result_bytes).hexdigest()
        or marker_path.stat().st_mtime_ns < result_path.stat().st_mtime_ns
    ):
        raise SandboxContractError(SandboxFailureReason.COMPLETION_MARKER_INVALID)
    export_payload = {
        "result_identity": result.result_identity,
        "marker_identity": marker.marker_identity,
        "result_sha256": sha256(result_bytes).hexdigest(),
        "marker_sha256": sha256(marker_bytes).hexdigest(),
    }
    return ValidatedBoundaryQualificationExport(
        result=result,
        completion=marker,
        export_identity=_identity(
            "windows-sandbox-boundary-result-export:v1", export_payload
        ),
    )


def _walk_files(root: Path) -> tuple[tuple[Path, os.stat_result], ...]:
    pending = [root]
    files: list[tuple[Path, os.stat_result]] = []
    while pending:
        directory = pending.pop()
        try:
            entries = sorted(os.scandir(directory), key=lambda item: item.name.casefold())
        except OSError as exc:
            raise SandboxContractError(SandboxFailureReason.PROTECTED_ROOT_MISSING) from exc
        for entry in entries:
            try:
                metadata = entry.stat(follow_symlinks=False)
            except OSError as exc:
                raise SandboxContractError(SandboxFailureReason.PROTECTED_ROOT_MISSING) from exc
            path = Path(entry.path)
            if _is_reparse(metadata):
                try:
                    target = path.resolve(strict=True)
                except OSError as exc:
                    raise SandboxContractError(
                        SandboxFailureReason.PROTECTED_ROOT_REPARSE_POINT
                    ) from exc
                if target != root and not target.is_relative_to(root):
                    raise SandboxContractError(
                        SandboxFailureReason.PROTECTED_ROOT_REPARSE_POINT
                    )
                files.append((path, metadata))
            elif stat.S_ISDIR(metadata.st_mode):
                pending.append(path)
            elif stat.S_ISREG(metadata.st_mode):
                files.append((path, metadata))
            else:
                raise SandboxContractError(SandboxFailureReason.PROTECTED_ROOT_KIND_MISMATCH)
    return tuple(sorted(files, key=lambda item: item[0].relative_to(root).as_posix()))


def capture_protected_host_inventory(
    manifest: ResolvedProtectedRootManifest,
    *,
    semantic_capture: Callable[[], dict[str, object]],
    immutable_verifier_identity: str,
) -> ProtectedHostInventory:
    """Hash every declared byte and obtain semantics from the independent verifier."""

    if tuple(binding.role for binding in manifest.bindings) != tuple(ProtectedRootRole):
        raise SandboxContractError(SandboxFailureReason.PROTECTED_ROOT_MANIFEST_INCOMPLETE)
    hash_cache: dict[str, ProtectedHostFile] = {}
    root_inventories: list[ProtectedRootInventory] = []
    for binding in manifest.bindings:
        try:
            root = binding.path.resolve(strict=True)
            root_metadata = root.stat(follow_symlinks=False)
        except OSError as exc:
            raise SandboxContractError(SandboxFailureReason.PROTECTED_ROOT_MISSING) from exc
        if _is_reparse(root_metadata):
            raise SandboxContractError(SandboxFailureReason.PROTECTED_ROOT_REPARSE_POINT)
        if binding.object_kind is ProtectedObjectKind.DIRECTORY:
            candidates = _walk_files(root)
        else:
            if not stat.S_ISREG(root_metadata.st_mode):
                raise SandboxContractError(SandboxFailureReason.PROTECTED_ROOT_KIND_MISMATCH)
            candidates = ((root, root_metadata),)
        rows: list[ProtectedRootFile] = []
        for path, _metadata in candidates:
            path_id = _path_identity(path)
            cached = hash_cache.get(path_id)
            if cached is None:
                byte_length, file_sha256 = _hash_stable_file(path)
                cached = ProtectedHostFile(
                    canonical_path_identity=path_id,
                    byte_length=byte_length,
                    sha256=file_sha256,
                )
                hash_cache[path_id] = cached
            relative_path = "." if path == root else path.relative_to(root).as_posix()
            rows.append(
                ProtectedRootFile(
                    relative_path=relative_path,
                    canonical_path_identity=path_id,
                    byte_length=cached.byte_length,
                    sha256=cached.sha256,
                )
            )
        root_payload = {
            "role": binding.role,
            "object_kind": binding.object_kind,
            "root_path_identity": _path_identity(root),
            "file_count": len(rows),
            "byte_length": sum(row.byte_length for row in rows),
            "files": [row.model_dump(mode="json") for row in rows],
        }
        root_inventories.append(
            ProtectedRootInventory(
                **root_payload,
                inventory_identity=_identity(
                    "windows-sandbox-protected-root-inventory:v1", root_payload
                ),
            )
        )
    semantic_inventory = semantic_capture()
    unique_files = tuple(
        sorted(hash_cache.values(), key=lambda item: item.canonical_path_identity)
    )
    payload = {
        "contract_version": PROTECTED_HOST_INVENTORY_VERSION,
        "protected_root_manifest_identity": manifest.manifest_identity,
        "immutable_verifier_identity": immutable_verifier_identity,
        "roots": [root.model_dump(mode="json") for root in root_inventories],
        "files": [item.model_dump(mode="json") for item in unique_files],
        "file_count": len(unique_files),
        "byte_length": sum(item.byte_length for item in unique_files),
        "semantic_inventory": semantic_inventory,
        "semantic_inventory_identity": _identity(
            "ticket06-immutable-semantic-inventory:v1", semantic_inventory
        ),
    }
    return ProtectedHostInventory(
        **payload,
        inventory_identity=_identity(
            "windows-sandbox-protected-host-inventory:v1", payload
        ),
    )


def compare_protected_host_inventories(
    before: ProtectedHostInventory,
    after: ProtectedHostInventory,
) -> HostInventoryDelta:
    old = {item.canonical_path_identity: item for item in before.files}
    new = {item.canonical_path_identity: item for item in after.files}
    changed_roles = tuple(
        root.role
        for root, current in zip(before.roots, after.roots, strict=True)
        if root.inventory_identity != current.inventory_identity
    )
    payload = {
        "contract_version": HOST_INVENTORY_DELTA_VERSION,
        "before_inventory_identity": before.inventory_identity,
        "after_inventory_identity": after.inventory_identity,
        "manifest_equal": (
            before.protected_root_manifest_identity
            == after.protected_root_manifest_identity
        ),
        "immutable_verifier_equal": (
            before.immutable_verifier_identity == after.immutable_verifier_identity
        ),
        "semantic_equal": before.semantic_inventory == after.semantic_inventory,
        "appended": len(new.keys() - old.keys()),
        "deleted": len(old.keys() - new.keys()),
        "rewritten": sum(
            old[key].byte_length != new[key].byte_length
            or old[key].sha256 != new[key].sha256
            for key in old.keys() & new.keys()
        ),
        "changed_roles": [role.value for role in changed_roles],
    }
    payload["equal"] = all(
        (
            payload["manifest_equal"],
            payload["immutable_verifier_equal"],
            payload["semantic_equal"],
            payload["appended"] == 0,
            payload["deleted"] == 0,
            payload["rewritten"] == 0,
            not changed_roles,
        )
    )
    return HostInventoryDelta(
        **payload,
        delta_identity=_identity("windows-sandbox-host-inventory-delta:v1", payload),
    )


def build_protected_writer_quiescence_contract(
    *,
    operator_attested: bool,
) -> ProtectedWriterQuiescenceContract:
    """Require an explicit operator assertion; absence never degrades the boundary."""

    if not operator_attested:
        raise SandboxContractError(SandboxFailureReason.PLATFORM_CONTRACT_INVALID)
    payload = {
        "contract_version": "windows-sandbox-protected-writer-quiescence-v1",
        "operator_attested": True,
        "scope": "sentinel-through-host-after",
        "protected_writers_permitted": False,
    }
    return ProtectedWriterQuiescenceContract(
        **payload,
        contract_identity=_identity(
            "windows-sandbox-protected-writer-quiescence:v1", payload
        ),
    )


def build_protected_writer_quiescence_evidence(
    *,
    contract: ProtectedWriterQuiescenceContract,
    sentinel: ProtectedHostInventory,
    immediate_before: ProtectedHostInventory,
) -> ProtectedWriterQuiescenceEvidence:
    prelaunch_delta = compare_protected_host_inventories(sentinel, immediate_before)
    if not prelaunch_delta.equal:
        raise SandboxContractError(SandboxFailureReason.RESULT_IDENTITY_MISMATCH)
    payload = {
        "contract": contract.model_dump(mode="json"),
        "sentinel_inventory_identity": sentinel.inventory_identity,
        "immediate_before_inventory_identity": immediate_before.inventory_identity,
        "prelaunch_delta": prelaunch_delta.model_dump(mode="json"),
    }
    return ProtectedWriterQuiescenceEvidence(
        contract=contract,
        sentinel_inventory_identity=sentinel.inventory_identity,
        immediate_before_inventory_identity=immediate_before.inventory_identity,
        prelaunch_delta=prelaunch_delta,
        evidence_identity=_identity(
            "windows-sandbox-protected-writer-quiescence-evidence:v1", payload
        ),
    )


def validate_verifier_root_ownership(inventory: ProtectedHostInventory) -> None:
    """Bind verifier semantics to the exact non-empty roots hashed by the host."""

    roots = {root.role: root for root in inventory.roots}
    semantic_bindings = (
        (ProtectedRootRole.CHECKPOINTS, "checkpoints"),
        (ProtectedRootRole.REPORTS_LOGS, "reports"),
        (ProtectedRootRole.TICKET07_EVIDENCE, "ticket07_evidence"),
    )
    for role, semantic_name in semantic_bindings:
        root = roots[role]
        semantic = inventory.semantic_inventory.get(semantic_name)
        if not isinstance(semantic, dict):
            raise SandboxContractError(SandboxFailureReason.RESULT_IDENTITY_MISMATCH)
        rows = [
            {
                "byte_length": item.byte_length,
                "relative_path": item.relative_path,
                "sha256": item.sha256,
            }
            for item in root.files
        ]
        expected_digest = sha256(_canonical_json_bytes(rows)).hexdigest()
        if (
            root.file_count == 0
            or semantic.get("file_count") != root.file_count
            or semantic.get("byte_length") != root.byte_length
            or semantic.get("inventory_sha256") != expected_digest
        ):
            raise SandboxContractError(SandboxFailureReason.RESULT_IDENTITY_MISMATCH)


class HistoricalISO03ReportContract(BaseModel):
    """Closed structural identities required to read one ISO-03 report generation."""

    model_config = _CLOSED_MODEL_CONFIG

    report_contract_version: Literal["iso03-host-qualification-report-v1"] = (
        "iso03-host-qualification-report-v1"
    )
    acceptance_structure_contract_version: Literal[
        "ticket06-acceptance-runner-structure-v2"
    ] = "ticket06-acceptance-runner-structure-v2"
    command_ledger_identity: str = Field(
        pattern=r"^ticket06-sandbox-command-ledger:v1:sha256:[0-9a-f]{64}$"
    )
    result_schema_identity: str = Field(
        pattern=(
            r"^windows-sandbox-ticket06-result-schema:v1:sha256:[0-9a-f]{64}$"
        )
    )
    completion_marker_contract_identity: str = Field(
        pattern=(
            r"^windows-sandbox-ticket06-completion-contract:v1:sha256:"
            r"[0-9a-f]{64}$"
        )
    )


_PRIOR_QUALIFIED_TICKET06_REPORT_CONTRACT = HistoricalISO03ReportContract(
    command_ledger_identity=(
        "ticket06-sandbox-command-ledger:v1:sha256:"
        "5444477692f8a4a517d6e78719c5d21d97550c6a08610bd88596ca76cb68ea23"
    ),
    result_schema_identity=(
        "windows-sandbox-ticket06-result-schema:v1:sha256:"
        "daabe8ef38f52c5580338a94f62fc3c15d710575db59e95de36f34e5dd98128e"
    ),
    completion_marker_contract_identity=(
        "windows-sandbox-ticket06-completion-contract:v1:sha256:"
        "7a6d7def1eddc7a14ae519ed87f851a0b180accaaa3e42aeab084e0a17dcc956"
    ),
)


@dataclass(frozen=True, slots=True)
class HistoricalQualificationReportPin:
    role: ProtectedRootRole
    run_id: str
    relative_path: str
    byte_length: int
    sha256: str
    report_identity: str
    current_iso03_schema: bool
    recorded_contract: HistoricalISO03ReportContract | None = None


_HISTORICAL_REPORT_PINS = (
    HistoricalQualificationReportPin(
        role=ProtectedRootRole.PRIOR_SANDBOX_QUALIFICATIONS,
        run_id="iso03-20260814-015",
        relative_path="iso03-20260814-015/host-audit/iso03-report.json",
        byte_length=15_111,
        sha256="fd074e13ff75e6915d76c4a620c3f579f856b7a957d377cf412897d2044ec9a8",
        report_identity=(
            "iso03-host-qualification-report:v1:sha256:"
            "bc0673e35050cb83c148b5766f082f526b146a6f73ba9c1a5138890742b1af22"
        ),
        current_iso03_schema=False,
    ),
    HistoricalQualificationReportPin(
        role=ProtectedRootRole.PRIOR_SANDBOX_QUALIFICATIONS,
        run_id="iso03-20260814-016",
        relative_path="iso03-20260814-016/host-audit/iso03-report.json",
        byte_length=21_824,
        sha256="09f70cda0e756d1af52432b83bdf73b1f013c4929a460f5f26973fabd291c056",
        report_identity=(
            "iso03-host-qualification-report:v1:sha256:"
            "28d08ceb2c87f4fbee820ab58c77dfcc96bdb7bb5d237e80e210c09a2bb1e77c"
        ),
        current_iso03_schema=False,
    ),
    HistoricalQualificationReportPin(
        role=ProtectedRootRole.REPLACEMENT_SANDBOX_QUALIFICATION,
        run_id="iso03-20260814-017",
        relative_path="iso03-20260814-017/host-audit/iso03-report.json",
        byte_length=21_985,
        sha256="d32759a2cc98867ecd6fb4b28ce926831a72aca397c990c3da02054316888ba1",
        report_identity=(
            "iso03-host-qualification-report:v1:sha256:"
            "fe08bc0196ad8b2c9297f78b44b2a29e4b0cfca0cf1cac5f5d7caf84ef3aeab1"
        ),
        current_iso03_schema=False,
    ),
    HistoricalQualificationReportPin(
        role=ProtectedRootRole.QUALIFIED_REPLACEMENT_SANDBOX_QUALIFICATION,
        run_id="iso03-20260814-018",
        relative_path="iso03-20260814-018/host-audit/iso03-report.json",
        byte_length=28_528,
        sha256="82af9d92f80d71b955fffb1005f9de60d18f6db008e31367ddcc2181d95af9f3",
        report_identity=(
            "iso03-host-qualification-report:v1:sha256:"
            "5a0f35c74299c1d5f0ee1220c3fd6fadaee481d91158c949fba951871f37922e"
        ),
        current_iso03_schema=True,
        recorded_contract=_PRIOR_QUALIFIED_TICKET06_REPORT_CONTRACT,
    ),
    HistoricalQualificationReportPin(
        role=ProtectedRootRole.FAILED_ISO04_TICKET06_ACCEPTANCE,
        run_id="iso04-20260815-ticket06-001",
        relative_path=(
            "iso04-20260815-ticket06-001/host-audit/"
            "iso04-ticket06-acceptance-report.json"
        ),
        byte_length=63_767,
        sha256="ede54c71f83f554a044604e2ae3ecb4c4d3100932b83a41ac7048ab66e369def",
        report_identity=(
            "iso04-ticket06-acceptance-report:v1:sha256:"
            "5f25988ca0d6776641628739963f03ef5e67a23d679800ac1213146893cb8dfe"
        ),
        current_iso03_schema=False,
    ),
    HistoricalQualificationReportPin(
        role=ProtectedRootRole.REVIEW_INVALIDATED_SANDBOX_QUALIFICATION,
        run_id="iso03-20260816-019",
        relative_path="iso03-20260816-019/host-audit/iso03-report.json",
        byte_length=28_516,
        sha256="cbffff23bf8e9e782fc100267db6dd949d7064e30013500aadb7c627c6d210aa",
        report_identity=(
            "iso03-host-qualification-report:v1:sha256:"
            "6a7a9a594a0968dcbaf32a61b86c27b5dda524362bf8a8a0b36c3168ffba2077"
        ),
        current_iso03_schema=True,
        recorded_contract=_PRIOR_QUALIFIED_TICKET06_REPORT_CONTRACT,
    ),
)


def load_historical_iso03_qualification_report(
    raw: bytes,
    *,
    recorded_contract: HistoricalISO03ReportContract,
) -> ISO03HostQualificationReport:
    """Read immutable ISO-03 evidence under its explicitly pinned contract."""

    try:
        report = validate_recorded_iso03_report_json(
            raw,
            command_ledger_identity=recorded_contract.command_ledger_identity,
            result_schema_identity=recorded_contract.result_schema_identity,
            completion_marker_contract_identity=(
                recorded_contract.completion_marker_contract_identity
            ),
        )
        static = report.static_bindings
        guest = report.guest_result
        if static is None or guest is None:
            raise ValueError("historical ISO-03 structural evidence is incomplete")
        structure = static.acceptance_runner_structure
        if (
            report.contract_version != recorded_contract.report_contract_version
            or structure.contract_version
            != recorded_contract.acceptance_structure_contract_version
            or structure.command_ledger_identity
            != recorded_contract.command_ledger_identity
            or structure.result_schema_identity
            != recorded_contract.result_schema_identity
            or structure.completion_marker_contract_identity
            != recorded_contract.completion_marker_contract_identity
        ):
            raise ValueError("historical ISO-03 recorded contract mismatch")
        host_harness = report.host_harness
        host_acceptance_module = (
            next(
                (
                    item
                    for item in host_harness.files
                    if item.relative_path
                    == "tradingagents/windows_sandbox_qualification.py"
                ),
                None,
            )
            if host_harness is not None
            else None
        )
        if (
            structure.boundary_runner_identity != static.runner_identity
            or structure.wsb_config_identity != static.wsb_config_identity
            or structure.mapping_manifest_identity
            != static.mapping_manifest_identity
            or report.runner_input_identity != structure.runner_input_identity
            or static.platform_qualification_identity
            != report.platform_qualification.qualification_identity
            or static.host_before_inventory_identity
            != report.host_before_inventory_identity
            or host_harness is None
            or host_acceptance_module is None
            or structure.host_harness_manifest_identity
            != host_harness.manifest_identity
            or structure.host_acceptance_module_sha256
            != host_acceptance_module.sha256
        ):
            raise ValueError("historical ISO-03 material identities disagree")
        static_payload = static.model_dump(mode="json")
        guest_static_payload = guest.model_dump(
            mode="json",
            include=set(QualificationStaticBindings.model_fields),
        )
        if guest_static_payload != static_payload:
            raise ValueError("historical ISO-03 static/result bindings disagree")
        if raw != _canonical_json_bytes(report.model_dump(mode="json")) + b"\n":
            raise ValueError("historical ISO-03 report is not canonical")
        return report
    except (TypeError, ValueError) as exc:
        raise SandboxContractError(
            SandboxFailureReason.PROTECTED_ROOT_EVIDENCE_INVALID
        ) from exc


def _validate_historical_report_pins(
    manifest: ResolvedProtectedRootManifest,
    pins: tuple[HistoricalQualificationReportPin, ...],
) -> None:
    by_role = {binding.role: binding.path for binding in manifest.bindings}
    roles = tuple(dict.fromkeys(pin.role for pin in pins))
    roots = tuple(by_role[role] for role in roles)
    if any(
        _paths_overlap(first, second)
        for index, first in enumerate(roots)
        for second in roots[index + 1 :]
    ):
        raise SandboxContractError(
            SandboxFailureReason.PROTECTED_ROOT_MANIFEST_OVERLAP
        )
    try:
        for role in roles:
            root = by_role[role]
            role_pins = tuple(pin for pin in pins if pin.role is role)
            expected_markers = tuple(
                sorted(root / Path(pin.relative_path) for pin in role_pins)
            )
            pattern = (
                "iso04-*/host-audit/iso04-ticket06-acceptance-report.json"
                if role is ProtectedRootRole.FAILED_ISO04_TICKET06_ACCEPTANCE
                else "iso03-*/host-audit/iso03-report.json"
            )
            if tuple(sorted(root.glob(pattern))) != expected_markers:
                raise ValueError("historical evidence marker set is incomplete")
            for pin, marker in zip(role_pins, expected_markers, strict=True):
                raw = _read_stable_regular_file(marker, maximum_bytes=1_048_576)
                if (
                    len(raw) != pin.byte_length
                    or sha256(raw).hexdigest() != pin.sha256
                    or pin.report_identity.encode("ascii") not in raw
                ):
                    raise ValueError("historical evidence report pin mismatch")
                if pin.current_iso03_schema:
                    if pin.recorded_contract is None:
                        raise ValueError("historical report contract pin is missing")
                    report = load_historical_iso03_qualification_report(
                        raw,
                        recorded_contract=pin.recorded_contract,
                    )
                    if (
                        report.run_id != pin.run_id
                        or report.report_identity != pin.report_identity
                    ):
                        raise ValueError(
                            "historical evidence closed report mismatch"
                        )
                elif pin.recorded_contract is not None:
                    raise ValueError("legacy report has a structural contract pin")
    except (SandboxContractError, OSError, RuntimeError, TypeError, ValueError) as exc:
        raise SandboxContractError(
            SandboxFailureReason.PROTECTED_ROOT_EVIDENCE_INVALID
        ) from exc
    if (
        len({pin.report_identity for pin in pins}) != len(pins)
        or len({pin.run_id for pin in pins}) != len(pins)
    ):
        raise SandboxContractError(
            SandboxFailureReason.PROTECTED_ROOT_EVIDENCE_INVALID
        )


def validate_iso03_historical_evidence_root_coverage(
    manifest: ResolvedProtectedRootManifest,
) -> None:
    """Require the exact closed current/legacy historical report set."""

    _validate_historical_report_pins(manifest, _HISTORICAL_REPORT_PINS)


def _validate_historical_report_pins_in_inventory(
    inventory: ProtectedHostInventory,
    pins: tuple[HistoricalQualificationReportPin, ...],
) -> None:
    """Bind the validated report allowlist to the authoritative sentinel rows."""

    roots = {root.role: root for root in inventory.roots}
    try:
        roles = tuple(dict.fromkeys(pin.role for pin in pins))
        for role in roles:
            role_pins = tuple(pin for pin in pins if pin.role is role)
            pattern = (
                "iso04-*/host-audit/iso04-ticket06-acceptance-report.json"
                if role is ProtectedRootRole.FAILED_ISO04_TICKET06_ACCEPTANCE
                else "iso03-*/host-audit/iso03-report.json"
            )
            rows = {
                row.relative_path: row
                for row in roots[role].files
            }
            marker_paths = {
                relative_path
                for relative_path in rows
                if PurePosixPath(relative_path).match(pattern)
            }
            if marker_paths != {pin.relative_path for pin in role_pins}:
                raise ValueError("historical evidence sentinel marker set mismatch")
            for pin in role_pins:
                row = rows[pin.relative_path]
                if row.byte_length != pin.byte_length or row.sha256 != pin.sha256:
                    raise ValueError("historical evidence sentinel pin mismatch")
    except (KeyError, TypeError, ValueError) as exc:
        raise SandboxContractError(
            SandboxFailureReason.PROTECTED_ROOT_EVIDENCE_INVALID
        ) from exc


_BOUNDARY_RUNNER_SOURCE_FILES = QUALIFIED_RUNNER_SOURCE_FILES


def _read_stable_regular_file(path: Path, *, maximum_bytes: int) -> bytes:
    try:
        before = path.lstat()
        if (
            not stat.S_ISREG(before.st_mode)
            or _is_reparse(before)
            or before.st_size > maximum_bytes
        ):
            raise OSError("unsafe bounded input")
        content = path.read_bytes()
        after = path.lstat()
    except OSError as exc:
        raise SandboxContractError(SandboxFailureReason.FORBIDDEN_INPUT) from exc
    if (
        before.st_size != after.st_size
        or before.st_mtime_ns != after.st_mtime_ns
        or len(content) != after.st_size
        or _is_reparse(after)
    ):
        raise SandboxContractError(SandboxFailureReason.FORBIDDEN_INPUT)
    return content


def _read_committed_git_object(
    repository: Path,
    arguments: tuple[str, ...],
    *,
    maximum_bytes: int,
) -> bytes:
    environment = {
        name: value
        for name, value in os.environ.items()
        if not name.upper().startswith("GIT_")
    }
    environment["GIT_TERMINAL_PROMPT"] = "0"
    try:
        completed = subprocess.run(
            (
                "git",
                "-c",
                "core.fsmonitor=false",
                "-c",
                "core.hooksPath=NUL",
                "-C",
                str(repository),
                *arguments,
            ),
            check=False,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            env=environment,
            shell=False,
            timeout=30,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise SandboxContractError(SandboxFailureReason.FORBIDDEN_INPUT) from exc
    if completed.returncode != 0 or len(completed.stdout) > maximum_bytes:
        raise SandboxContractError(SandboxFailureReason.FORBIDDEN_INPUT)
    return completed.stdout


def _committed_git_hex(
    repository: Path,
    revision: str,
) -> str:
    raw = _read_committed_git_object(
        repository,
        ("rev-parse", "--verify", revision),
        maximum_bytes=128,
    )
    try:
        value = raw.decode("ascii").strip()
    except UnicodeDecodeError as exc:
        raise SandboxContractError(SandboxFailureReason.FORBIDDEN_INPUT) from exc
    if re.fullmatch(r"[0-9a-f]{40}", value) is None:
        raise SandboxContractError(SandboxFailureReason.FORBIDDEN_INPUT)
    return value


def _reviewed_index_tree_hash(repository: Path) -> str:
    raw = _read_committed_git_object(
        repository,
        ("write-tree",),
        maximum_bytes=128,
    )
    try:
        value = raw.decode("ascii").strip()
    except UnicodeDecodeError as exc:
        raise SandboxContractError(SandboxFailureReason.FORBIDDEN_INPUT) from exc
    if re.fullmatch(r"[0-9a-f]{40}", value) is None:
        raise SandboxContractError(SandboxFailureReason.FORBIDDEN_INPUT)
    return value


def build_reviewed_host_harness_manifest(
    repository_root: Path,
    *,
    entrypoint_path: Path,
    loaded_paths: dict[str, Path] | None = None,
) -> ReviewedHostHarnessManifest:
    """Bind executing host authority to the explicit reviewed Git index bytes."""

    repository = _resolve_safe_directory(
        repository_root,
        reason=SandboxFailureReason.SOURCE_REPOSITORY_INVALID,
    )
    expected_paths = {
        "scripts/run_iso03_windows_sandbox_qualification.py": entrypoint_path,
        "tradingagents/offline_runtime_bundle.py": Path(
            offline_runtime_bundle_module.__file__
        ),
        "tradingagents/windows_sandbox_acceptance.py": Path(
            windows_sandbox_acceptance_module.__file__
        ),
        "tradingagents/windows_sandbox_qualification.py": Path(__file__),
        "tradingagents/windows_sandbox_runner.py": Path(
            windows_sandbox_runner_module.__file__
        ),
    }
    if loaded_paths is not None:
        if set(loaded_paths) != set(expected_paths):
            raise SandboxContractError(SandboxFailureReason.FORBIDDEN_INPUT)
        expected_paths = loaded_paths
    base_commit_id = _committed_git_hex(repository, "HEAD^{commit}")
    candidate_tree_hash = _reviewed_index_tree_hash(repository)
    rows: list[ReviewedHostHarnessFile] = []
    for relative_path in sorted(expected_paths):
        expected_loaded_path = (repository / relative_path).resolve(strict=False)
        try:
            actual_loaded_path = expected_paths[relative_path].resolve(strict=True)
        except OSError as exc:
            raise SandboxContractError(SandboxFailureReason.FORBIDDEN_INPUT) from exc
        if actual_loaded_path != expected_loaded_path:
            raise SandboxContractError(SandboxFailureReason.FORBIDDEN_INPUT)
        loaded = _read_stable_regular_file(
            actual_loaded_path,
            maximum_bytes=4 * 1024 * 1024,
        )
        reviewed = _read_committed_git_object(
            repository,
            ("show", f":{relative_path}"),
            maximum_bytes=4 * 1024 * 1024,
        )
        if not reviewed or loaded != reviewed:
            raise SandboxContractError(SandboxFailureReason.FORBIDDEN_INPUT)
        rows.append(
            ReviewedHostHarnessFile(
                relative_path=relative_path,
                byte_length=len(reviewed),
                sha256=sha256(reviewed).hexdigest(),
            )
        )
    if (
        _committed_git_hex(repository, "HEAD^{commit}") != base_commit_id
        or _reviewed_index_tree_hash(repository) != candidate_tree_hash
    ):
        raise SandboxContractError(SandboxFailureReason.FORBIDDEN_INPUT)
    payload = {
        "contract_version": "iso03-reviewed-host-harness-v1",
        "implementation_base_commit_id": base_commit_id,
        "candidate_tree_hash": candidate_tree_hash,
        "source_mode": "reviewed_index",
        "files": [item.model_dump(mode="json") for item in rows],
    }
    return ReviewedHostHarnessManifest(
        **payload,
        manifest_identity=_identity("iso03-reviewed-host-harness:v1", payload),
    )


def stage_boundary_runner_input(
    repository_root: Path,
    destination: Path,
) -> BoundaryRunnerManifest:
    """Stage only explicitly reviewed qualification runner blobs from the index."""

    try:
        repository = repository_root.resolve(strict=True)
        if not repository.is_dir() or _is_reparse(repository.lstat()):
            raise OSError("unsafe repository")
        destination.mkdir(exist_ok=False)
    except OSError as exc:
        raise SandboxContractError(SandboxFailureReason.FORBIDDEN_INPUT) from exc
    base_commit_id = _committed_git_hex(repository, "HEAD^{commit}")
    candidate_tree_hash = _reviewed_index_tree_hash(repository)
    rows: list[BoundaryRunnerFile] = []
    for relative_path, target_name in _BOUNDARY_RUNNER_SOURCE_FILES:
        content = _read_committed_git_object(
            repository,
            ("show", f":{relative_path}"),
            maximum_bytes=1_048_576,
        )
        if not content:
            raise SandboxContractError(SandboxFailureReason.FORBIDDEN_INPUT)
        target = destination / target_name
        try:
            with target.open("xb") as handle:
                handle.write(content)
        except OSError as exc:
            raise SandboxContractError(SandboxFailureReason.FORBIDDEN_INPUT) from exc
        rows.append(
            QualifiedRunnerFile(
                relative_path=target_name,
                byte_length=len(content),
                sha256=sha256(content).hexdigest(),
            )
        )
    payload = {
        "contract_version": "windows-sandbox-qualified-runner-input-v2",
        "implementation_base_commit_id": base_commit_id,
        "candidate_tree_hash": candidate_tree_hash,
        "source_mode": "reviewed_index",
        "files": [item.model_dump(mode="json") for item in rows],
    }
    manifest = QualifiedRunnerManifest(
        **payload,
        runner_input_identity=acceptance_content_identity(
            "windows-sandbox-qualified-runner-input:v2", payload
        ),
    )
    try:
        (destination / "runner-manifest.json").write_bytes(
            _canonical_json_bytes(manifest.model_dump(mode="json")) + b"\n"
        )
    except OSError as exc:
        raise SandboxContractError(SandboxFailureReason.FORBIDDEN_INPUT) from exc
    if (
        _committed_git_hex(repository, "HEAD^{commit}") != base_commit_id
        or _reviewed_index_tree_hash(repository) != candidate_tree_hash
    ):
        raise SandboxContractError(SandboxFailureReason.FORBIDDEN_INPUT)
    return manifest


def validate_boundary_runner_input(
    root: Path,
    *,
    expected: BoundaryRunnerManifest,
) -> None:
    try:
        resolved = root.resolve(strict=True)
        entries = tuple(sorted(resolved.iterdir(), key=lambda item: item.name))
    except OSError as exc:
        raise SandboxContractError(SandboxFailureReason.MAPPING_CONTRACT_INVALID) from exc
    expected_names = tuple(sorted((*QUALIFIED_RUNNER_TARGET_FILES, "runner-manifest.json")))
    if tuple(item.name for item in entries) != expected_names:
        raise SandboxContractError(SandboxFailureReason.MAPPING_CONTRACT_INVALID)
    try:
        parsed = QualifiedRunnerManifest.model_validate_json(
            _read_stable_regular_file(
                resolved / "runner-manifest.json", maximum_bytes=64 * 1024
            )
        )
    except ValueError as exc:
        raise SandboxContractError(SandboxFailureReason.MAPPING_CONTRACT_INVALID) from exc
    if parsed != expected:
        raise SandboxContractError(SandboxFailureReason.MAPPING_CONTRACT_INVALID)
    for row in expected.files:
        content = _read_stable_regular_file(
            resolved / row.relative_path,
            maximum_bytes=1_048_576,
        )
        if len(content) != row.byte_length or sha256(content).hexdigest() != row.sha256:
            raise SandboxContractError(SandboxFailureReason.MAPPING_CONTRACT_INVALID)


def copy_verified_runtime_bundle(
    source_root: Path,
    destination: Path,
    *,
    source: ReviewedSourceIdentity,
) -> tuple[RuntimeBundleInputContract, str]:
    """Copy the ISO-02 bundle outside the repository and verify both copies."""

    source_binding = OfflineBundleSourceBinding(
        contract_version=source.contract_version,
        commit_id=source.commit_id,
        tree_hash=source.tree_hash,
        source_input_identity=source.source_input_identity,
        source_manifest_identity=source.source_manifest_identity,
        source_archive_sha256=source.archive_sha256,
    )
    try:
        sealed_source = source_root.resolve(strict=True)
        if not sealed_source.is_dir() or _is_reparse(sealed_source.lstat()):
            raise OSError("unsafe runtime bundle root")
        _walk_files(sealed_source)
        source_manifest = verify_offline_runtime_bundle(
            sealed_source,
            expected_source=source_binding,
        )
        shutil.copytree(sealed_source, destination, copy_function=shutil.copyfile)
        copied_manifest = verify_offline_runtime_bundle(
            destination,
            expected_source=source_binding,
        )
        source_manifest_repeated = verify_offline_runtime_bundle(
            sealed_source,
            expected_source=source_binding,
        )
        manifest_bytes = (destination / BUNDLE_MANIFEST_FILENAME).read_bytes()
    except (OSError, OfflineBundleContractError, ValueError) as exc:
        raise SandboxContractError(SandboxFailureReason.RUNTIME_BUNDLE_NOT_READY) from exc
    if copied_manifest != source_manifest or source_manifest_repeated != source_manifest:
        raise SandboxContractError(SandboxFailureReason.RUNTIME_BUNDLE_NOT_READY)
    return (
        build_sealed_runtime_bundle_input(
            copied_manifest,
            manifest_sha256=sha256(manifest_bytes).hexdigest(),
        ),
        copied_manifest.bundle_identity,
    )


def _directory_probe_target(
    manifest: ResolvedProtectedRootManifest,
    role: ProtectedRootRole,
    *,
    run_id: str,
) -> Path:
    binding = next(item for item in manifest.bindings if item.role is role)
    if binding.object_kind is not ProtectedObjectKind.DIRECTORY:
        raise SandboxContractError(SandboxFailureReason.PROTECTED_ROOT_KIND_MISMATCH)
    files = _walk_files(binding.path)
    if files:
        return files[0][0]
    return binding.path / f".iso03-{run_id}-write-probe"


def validate_adversarial_probe_targets(
    targets: tuple[AdversarialProbeTarget, ...],
    *,
    manifest: ResolvedProtectedRootManifest,
) -> None:
    if tuple(item.mechanism for item in targets) != tuple(ProbeMechanism):
        raise SandboxContractError(SandboxFailureReason.MAPPING_CONTRACT_INVALID)
    by_role = {binding.role: binding for binding in manifest.bindings}
    for target in targets:
        candidate = Path(target.target_host_path).resolve(strict=False)
        covered = any(
            candidate == binding.path
            or binding.object_kind is ProtectedObjectKind.DIRECTORY
            and candidate.is_relative_to(binding.path)
            for binding in manifest.bindings
        )
        if not covered or target.target_path_identity != host_path_identity(candidate):
            raise SandboxContractError(SandboxFailureReason.PROTECTED_ROOT_MANIFEST_INCOMPLETE)
    exact_targets = {
        ProbeMechanism.PYTHON_ISOLATED: by_role[
            ProtectedRootRole.ISOLATED_PYTHON_WRITE_PROBE
        ].path,
        ProbeMechanism.SQLITE: by_role[ProtectedRootRole.PROVIDER_LEDGER].path,
    }
    actual = {item.mechanism: Path(item.target_host_path) for item in targets}
    if any(actual[mechanism] != path for mechanism, path in exact_targets.items()):
        raise SandboxContractError(SandboxFailureReason.MAPPING_CONTRACT_INVALID)


def select_adversarial_probe_targets(
    manifest: ResolvedProtectedRootManifest,
    *,
    run_id: str,
) -> tuple[AdversarialProbeTarget, ...]:
    """Spread the nine required mechanisms over protected path classes."""

    by_role = {binding.role: binding for binding in manifest.bindings}
    paths = {
        ProbeMechanism.NORMAL_PYTHON: _directory_probe_target(
            manifest, ProtectedRootRole.PRIOR_INVENTORIES, run_id=run_id
        ),
        ProbeMechanism.PYTHON_ISOLATED: by_role[
            ProtectedRootRole.ISOLATED_PYTHON_WRITE_PROBE
        ].path,
        ProbeMechanism.PYTHON_NO_SITE: by_role[
            ProtectedRootRole.CALENDAR_DATABASE
        ].path,
        ProbeMechanism.PYTHON_IGNORE_ENVIRONMENT: _directory_probe_target(
            manifest, ProtectedRootRole.CALENDAR_PAYLOADS, run_id=run_id
        ),
        ProbeMechanism.MULTIPROCESSING_SPAWN: by_role[
            ProtectedRootRole.PROVIDER_LEDGER_WAL
        ].path,
        ProbeMechanism.POWERSHELL: _directory_probe_target(
            manifest, ProtectedRootRole.REPORTS_LOGS, run_id=run_id
        ),
        ProbeMechanism.CMD: _directory_probe_target(
            manifest, ProtectedRootRole.CHECKPOINTS, run_id=run_id
        ),
        ProbeMechanism.DIRECT_FILESYSTEM: _directory_probe_target(
            manifest, ProtectedRootRole.TICKET07_EVIDENCE, run_id=run_id
        ),
        ProbeMechanism.SQLITE: by_role[ProtectedRootRole.PROVIDER_LEDGER].path,
    }
    targets = tuple(
        AdversarialProbeTarget(
            mechanism=mechanism,
            target_host_path=str(paths[mechanism]),
            target_path_identity=host_path_identity(paths[mechanism]),
        )
        for mechanism in ProbeMechanism
    )
    validate_adversarial_probe_targets(targets, manifest=manifest)
    return targets


_BOUNDARY_MAPPING_GUEST_PATHS = {
    SandboxMappingRole.SOURCE: r"C:\QCF17\Input\Source",
    SandboxMappingRole.RUNNER: r"C:\QCF17\Input\Runner",
    SandboxMappingRole.CONTROL: r"C:\QCF17\Input\Control",
    SandboxMappingRole.RUNTIME_BUNDLE: r"C:\QCF17\Input\Runtime",
    SandboxMappingRole.RESULTS_EXPORT: r"C:\QCF17\Export",
}


@dataclass(frozen=True, slots=True)
class BoundaryQualificationConstructionRequest:
    implementation_repository_root: Path
    implementation_base_commit_id: str
    candidate_tree_hash: str
    host_harness: ReviewedHostHarnessManifest
    host_entrypoint_path: Path
    reviewed_source_repository_root: Path
    protected_state_repository_root: Path
    runs_root: Path
    runtime_bundle_root: Path
    run_id: str
    user_profile: Path
    host_temp: Path
    host_venv: Path
    protected_manifest: ResolvedProtectedRootManifest
    platform_qualification: WindowsHostPlatformQualification
    host_before_inventory: ProtectedHostInventory
    protected_writer_quiescence: ProtectedWriterQuiescenceContract
    immutable_verifier_identity: str
    probe_targets: tuple[AdversarialProbeTarget, ...]


@dataclass(frozen=True, slots=True)
class BoundaryQualificationRunPaths:
    run_root: Path
    source_input: Path
    runner_input: Path
    control_input: Path
    runtime_input: Path
    results_export: Path
    audit_root: Path
    config_path: Path


@dataclass(frozen=True, slots=True)
class BoundaryQualificationRunContract:
    platform_report: SandboxPlatformReport
    platform_qualification: WindowsHostPlatformQualification
    paths: BoundaryQualificationRunPaths
    source: StagedSourceInput
    runtime_bundle: RuntimeBundleInputContract
    runtime_bundle_identity: str
    runner_input: BoundaryRunnerManifest
    host_harness: ReviewedHostHarnessManifest
    implementation_repository_root: Path
    host_entrypoint_path: Path
    protected_manifest: ResolvedProtectedRootManifest
    mapping_manifest: ResolvedSandboxMappingManifest
    wsb_configuration: WsbConfiguration
    guest_layout: GuestLayout
    environment_policy: EnvironmentPolicy
    control: BoundaryQualificationControlEnvelope
    static_bindings: QualificationStaticBindings
    probe_targets: tuple[AdversarialProbeTarget, ...]
    forbidden_host_roots: tuple[Path, ...]


@dataclass(frozen=True, slots=True)
class BoundaryQualificationExecution:
    process: SandboxProcessResult
    export: ValidatedBoundaryQualificationExport


@dataclass(frozen=True, slots=True)
class Ticket06AcceptanceConstructionRequest:
    """Acceptance construction reuses the exact qualified runner inputs."""

    qualified_runner_request: BoundaryQualificationConstructionRequest
    boundary_qualification_identity: str
    frozen_baseline_identity: str


@dataclass(frozen=True, slots=True)
class Ticket06AcceptanceRunContract:
    platform_report: SandboxPlatformReport
    paths: BoundaryQualificationRunPaths
    source: StagedSourceInput
    runtime_bundle: RuntimeBundleInputContract
    runtime_bundle_identity: str
    runner_input: BoundaryRunnerManifest
    host_harness: ReviewedHostHarnessManifest
    implementation_repository_root: Path
    host_entrypoint_path: Path
    protected_manifest: ResolvedProtectedRootManifest
    mapping_manifest: ResolvedSandboxMappingManifest
    wsb_configuration: WsbConfiguration
    guest_layout: GuestLayout
    environment_policy: Ticket06ControlEnvironment
    control: Ticket06AcceptanceControlEnvelope
    forbidden_host_roots: tuple[Path, ...]


@dataclass(frozen=True, slots=True)
class Ticket06AcceptanceExecution:
    sandbox_process_evidence: ISO04SandboxProcessEvidence
    export: ValidatedTicket06AcceptanceExport

    @property
    def process(self) -> SandboxProcessResult:
        """Compatibility projection sourced only from persisted host evidence."""

        return self.sandbox_process_evidence.to_process_result()


class ISO04SandboxProcessEvidence(BaseModel):
    """Durable host observation written before ISO-04 report synthesis."""

    model_config = _CLOSED_MODEL_CONFIG

    contract_version: Literal["iso04-sandbox-process-evidence-v1"] = (
        "iso04-sandbox-process-evidence-v1"
    )
    run_id: str = Field(pattern=r"^[a-z0-9][a-z0-9._-]{7,63}$")
    acceptance_control_identity: str = Field(
        pattern=(
            r"^windows-sandbox-ticket06-control:v1:sha256:[0-9a-f]{64}$"
        )
    )
    outer_sandbox_process_returncode: int = Field(
        ge=-(2**31),
        le=(2**32) - 1,
    )
    timed_out: bool
    termination_confirmed: bool
    before_processes: tuple[SandboxProcessTopologyEntry, ...]
    launched_processes: tuple[SandboxProcessTopologyEntry, ...]
    after_processes: tuple[SandboxProcessTopologyEntry, ...]
    evidence_identity: str = Field(
        pattern=r"^iso04-sandbox-process-evidence:v1:sha256:[0-9a-f]{64}$"
    )

    @model_validator(mode="after")
    def _validate_evidence(self) -> ISO04SandboxProcessEvidence:
        for rows in (
            self.before_processes,
            self.launched_processes,
            self.after_processes,
        ):
            if rows != WindowsSandboxPlatform._ordered_topology(rows) or len(
                {item.process_id for item in rows}
            ) != len(rows):
                raise ValueError("ISO-04 Sandbox process topology is invalid")
        payload = self.model_dump(mode="json", exclude={"evidence_identity"})
        if self.evidence_identity != _identity(
            "iso04-sandbox-process-evidence:v1",
            payload,
        ):
            raise ValueError("ISO-04 Sandbox process evidence identity mismatch")
        return self

    @classmethod
    def create(
        cls,
        *,
        run_id: str,
        acceptance_control_identity: str,
        process: SandboxProcessResult,
    ) -> ISO04SandboxProcessEvidence:
        payload = {
            "contract_version": "iso04-sandbox-process-evidence-v1",
            "run_id": run_id,
            "acceptance_control_identity": acceptance_control_identity,
            "outer_sandbox_process_returncode": process.returncode,
            "timed_out": process.timed_out,
            "termination_confirmed": process.termination_confirmed,
            "before_processes": [
                item.model_dump(mode="json") for item in process.before_processes
            ],
            "launched_processes": [
                item.model_dump(mode="json") for item in process.launched_processes
            ],
            "after_processes": [
                item.model_dump(mode="json") for item in process.after_processes
            ],
        }
        return cls(
            **payload,
            evidence_identity=_identity(
                "iso04-sandbox-process-evidence:v1",
                payload,
            ),
        )

    def to_process_result(self) -> SandboxProcessResult:
        return SandboxProcessResult(
            returncode=self.outer_sandbox_process_returncode,
            timed_out=self.timed_out,
            before_processes=self.before_processes,
            launched_processes=self.launched_processes,
            after_processes=self.after_processes,
            termination_confirmed=self.termination_confirmed,
        )


class ISO04Ticket06OperationalEvidence(BaseModel):
    """Bounded authoritative host input for later ISO-04 report synthesis."""

    model_config = _CLOSED_MODEL_CONFIG

    contract_version: Literal["iso04-ticket06-operational-evidence-v1"] = (
        "iso04-ticket06-operational-evidence-v1"
    )
    sandbox_process: ISO04SandboxProcessEvidence
    guest_result_identity: str | None = Field(
        default=None,
        pattern=r"^windows-sandbox-ticket06-result:v1:sha256:[0-9a-f]{64}$",
    )
    completion_marker_identity: str | None = Field(
        default=None,
        pattern=(
            r"^windows-sandbox-ticket06-completion:v1:sha256:[0-9a-f]{64}$"
        ),
    )
    guest_export_identity: str | None = Field(
        default=None,
        pattern=(
            r"^windows-sandbox-ticket06-result-export:v1:sha256:[0-9a-f]{64}$"
        ),
    )
    host_before_inventory_identity: str = Field(
        pattern=(
            r"^windows-sandbox-protected-host-inventory:v1:sha256:[0-9a-f]{64}$"
        )
    )
    host_after_inventory_identity: str = Field(
        pattern=(
            r"^windows-sandbox-protected-host-inventory:v1:sha256:[0-9a-f]{64}$"
        )
    )
    command_failure_identity: str | None = Field(
        default=None,
        pattern=r"^ticket06-command-result:v1:sha256:[0-9a-f]{64}$",
    )
    operational_evidence_identity: str = Field(
        pattern=(
            r"^iso04-ticket06-operational-evidence:v1:sha256:[0-9a-f]{64}$"
        )
    )

    @model_validator(mode="after")
    def _validate_evidence(self) -> ISO04Ticket06OperationalEvidence:
        guest_identities = (
            self.guest_result_identity,
            self.completion_marker_identity,
            self.guest_export_identity,
        )
        if any(item is None for item in guest_identities) and any(
            item is not None for item in guest_identities
        ):
            raise ValueError("ISO-04 guest evidence identities are incomplete")
        if self.command_failure_identity is not None and self.guest_result_identity is None:
            raise ValueError("ISO-04 command failure lacks guest result authority")
        payload = self.model_dump(
            mode="json",
            exclude={"operational_evidence_identity"},
        )
        if self.operational_evidence_identity != _identity(
            "iso04-ticket06-operational-evidence:v1",
            payload,
        ):
            raise ValueError("ISO-04 operational evidence identity mismatch")
        return self

    @classmethod
    def create(
        cls,
        *,
        sandbox_process: ISO04SandboxProcessEvidence,
        export: ValidatedTicket06AcceptanceExport | None,
        host_before_inventory_identity: str,
        host_after_inventory_identity: str,
    ) -> ISO04Ticket06OperationalEvidence:
        command_failure_identity = None
        if export is not None:
            failed_command = next(
                (
                    item
                    for item in export.result.command_results
                    if item.status
                    in {
                        AcceptanceCommandStatus.FAIL,
                        AcceptanceCommandStatus.TIMEOUT,
                    }
                ),
                None,
            )
            if failed_command is not None:
                command_failure_identity = failed_command.summary_identity
        payload = {
            "contract_version": "iso04-ticket06-operational-evidence-v1",
            "sandbox_process": sandbox_process.model_dump(mode="json"),
            "guest_result_identity": (
                export.result.result_identity if export is not None else None
            ),
            "completion_marker_identity": (
                export.completion.marker_identity if export is not None else None
            ),
            "guest_export_identity": (
                export.export_identity if export is not None else None
            ),
            "host_before_inventory_identity": host_before_inventory_identity,
            "host_after_inventory_identity": host_after_inventory_identity,
            "command_failure_identity": command_failure_identity,
        }
        return cls(
            **payload,
            operational_evidence_identity=_identity(
                "iso04-ticket06-operational-evidence:v1",
                payload,
            ),
        )


class ISO04Ticket06AcceptanceReport(BaseModel):
    """Closed host report envelope derived from durable ISO-04 authorities."""

    model_config = _CLOSED_MODEL_CONFIG

    contract_version: Literal["iso04-ticket06-acceptance-report-v1"] = (
        "iso04-ticket06-acceptance-report-v1"
    )
    operational_evidence: ISO04Ticket06OperationalEvidence
    report_identity: str = Field(
        pattern=r"^iso04-ticket06-acceptance-report:v1:sha256:[0-9a-f]{64}$"
    )

    @model_validator(mode="after")
    def _validate_report(self) -> ISO04Ticket06AcceptanceReport:
        payload = self.model_dump(mode="json", exclude={"report_identity"})
        if self.report_identity != _identity(
            "iso04-ticket06-acceptance-report:v1",
            payload,
        ):
            raise ValueError("ISO-04 Ticket 06 report identity mismatch")
        return self

    @classmethod
    def create(
        cls,
        evidence: ISO04Ticket06OperationalEvidence,
    ) -> ISO04Ticket06AcceptanceReport:
        payload = {
            "contract_version": "iso04-ticket06-acceptance-report-v1",
            "operational_evidence": evidence.model_dump(mode="json"),
        }
        return cls(
            **payload,
            report_identity=_identity(
                "iso04-ticket06-acceptance-report:v1",
                payload,
            ),
        )


def load_iso04_sandbox_process_evidence(
    path: Path,
    *,
    expected_run_id: str,
    expected_acceptance_control_identity: str,
) -> ISO04SandboxProcessEvidence:
    """Load the canonical host process checkpoint for later report synthesis."""

    content = _read_stable_regular_file(
        path,
        maximum_bytes=_ISO04_SANDBOX_PROCESS_EVIDENCE_MAXIMUM_BYTES,
    )
    try:
        evidence = ISO04SandboxProcessEvidence.model_validate_json(content)
    except ValueError as exc:
        raise SandboxContractError(
            SandboxFailureReason.RESULT_IDENTITY_MISMATCH
        ) from exc
    if (
        evidence.run_id != expected_run_id
        or evidence.acceptance_control_identity
        != expected_acceptance_control_identity
        or content
        != _canonical_json_bytes(evidence.model_dump(mode="json")) + b"\n"
    ):
        raise SandboxContractError(SandboxFailureReason.RESULT_IDENTITY_MISMATCH)
    return evidence


def persist_iso04_sandbox_process_evidence(
    *,
    audit_root: Path,
    run_id: str,
    acceptance_control_identity: str,
    process: SandboxProcessResult,
) -> ISO04SandboxProcessEvidence:
    """Durably checkpoint the exact outer rc before any derived host report."""

    evidence = ISO04SandboxProcessEvidence.create(
        run_id=run_id,
        acceptance_control_identity=acceptance_control_identity,
        process=process,
    )
    path = audit_root / ISO04_SANDBOX_PROCESS_EVIDENCE_FILENAME
    _write_host_audit(path, evidence)
    return load_iso04_sandbox_process_evidence(
        path,
        expected_run_id=run_id,
        expected_acceptance_control_identity=acceptance_control_identity,
    )


def load_iso04_ticket06_operational_evidence(
    path: Path,
    *,
    expected_run_id: str,
    expected_acceptance_control_identity: str,
) -> ISO04Ticket06OperationalEvidence:
    """Load the canonical bounded evidence consumed by ISO-04 reporting."""

    content = _read_stable_regular_file(
        path,
        maximum_bytes=_ISO04_TICKET06_OPERATIONAL_EVIDENCE_MAXIMUM_BYTES,
    )
    try:
        evidence = ISO04Ticket06OperationalEvidence.model_validate_json(content)
    except ValueError as exc:
        raise SandboxContractError(
            SandboxFailureReason.RESULT_IDENTITY_MISMATCH
        ) from exc
    if (
        evidence.sandbox_process.run_id != expected_run_id
        or evidence.sandbox_process.acceptance_control_identity
        != expected_acceptance_control_identity
        or content
        != _canonical_json_bytes(evidence.model_dump(mode="json")) + b"\n"
    ):
        raise SandboxContractError(SandboxFailureReason.RESULT_IDENTITY_MISMATCH)
    return evidence


def persist_iso04_ticket06_operational_evidence(
    *,
    audit_root: Path,
    expected_run_id: str,
    expected_acceptance_control_identity: str,
    export: ValidatedTicket06AcceptanceExport | None,
    host_before_inventory_identity: str,
    host_after_inventory_identity: str,
) -> ISO04Ticket06OperationalEvidence:
    """Bind persisted process state and later authorities before report writing."""

    sandbox_process = load_iso04_sandbox_process_evidence(
        audit_root / ISO04_SANDBOX_PROCESS_EVIDENCE_FILENAME,
        expected_run_id=expected_run_id,
        expected_acceptance_control_identity=(
            expected_acceptance_control_identity
        ),
    )
    evidence = ISO04Ticket06OperationalEvidence.create(
        sandbox_process=sandbox_process,
        export=export,
        host_before_inventory_identity=host_before_inventory_identity,
        host_after_inventory_identity=host_after_inventory_identity,
    )
    path = audit_root / ISO04_TICKET06_OPERATIONAL_EVIDENCE_FILENAME
    _write_host_audit(path, evidence)
    return load_iso04_ticket06_operational_evidence(
        path,
        expected_run_id=expected_run_id,
        expected_acceptance_control_identity=(
            expected_acceptance_control_identity
        ),
    )


def write_iso04_ticket06_report(
    *,
    audit_root: Path,
    expected_run_id: str,
    expected_acceptance_control_identity: str,
    export: ValidatedTicket06AcceptanceExport | None,
    host_before_inventory_identity: str,
    host_after_inventory_identity: str,
    report_builder: Callable[
        [ISO04Ticket06OperationalEvidence],
        ISO04Ticket06AcceptanceReport,
    ],
) -> ISO04Ticket06AcceptanceReport:
    """Persist authorities first, then build one canonical ISO-04 report."""

    evidence = persist_iso04_ticket06_operational_evidence(
        audit_root=audit_root,
        expected_run_id=expected_run_id,
        expected_acceptance_control_identity=(
            expected_acceptance_control_identity
        ),
        export=export,
        host_before_inventory_identity=host_before_inventory_identity,
        host_after_inventory_identity=host_after_inventory_identity,
    )
    candidate = report_builder(evidence)
    if not isinstance(candidate, ISO04Ticket06AcceptanceReport):
        raise SandboxContractError(SandboxFailureReason.RESULT_IDENTITY_MISMATCH)
    try:
        report = ISO04Ticket06AcceptanceReport.model_validate(
            candidate.model_dump(mode="json")
        )
    except (TypeError, ValueError) as exc:
        raise SandboxContractError(
            SandboxFailureReason.RESULT_IDENTITY_MISMATCH
        ) from exc
    if report.operational_evidence != evidence:
        raise SandboxContractError(SandboxFailureReason.RESULT_IDENTITY_MISMATCH)
    _write_host_audit(
        audit_root / ISO04_TICKET06_REPORT_FILENAME,
        report,
    )
    return report


def _paths_overlap(first: Path, second: Path) -> bool:
    return first == second or first.is_relative_to(second) or second.is_relative_to(first)


def _resolve_safe_directory(path: Path, *, reason: SandboxFailureReason) -> Path:
    try:
        resolved = path.resolve(strict=True)
        metadata = resolved.lstat()
    except OSError as exc:
        raise SandboxContractError(reason) from exc
    if not resolved.is_dir() or _is_reparse(metadata):
        raise SandboxContractError(reason)
    return resolved


def _validate_staged_source(source: StagedSourceInput, source_root: Path) -> None:
    try:
        root = source_root.resolve(strict=True)
        entries = tuple(sorted(root.iterdir(), key=lambda item: item.name))
    except OSError as exc:
        raise SandboxContractError(SandboxFailureReason.SOURCE_IDENTITY_MISMATCH) from exc
    if tuple(item.name for item in entries) != (
        "reviewed-commit.object",
        "reviewed-source.tar",
        "source-manifest.json",
    ):
        raise SandboxContractError(SandboxFailureReason.SOURCE_IDENTITY_MISMATCH)
    archive = _read_stable_regular_file(
        root / "reviewed-source.tar", maximum_bytes=512 * 1024 * 1024
    )
    commit = _read_stable_regular_file(
        root / "reviewed-commit.object", maximum_bytes=4 * 1024 * 1024
    )
    manifest = _read_stable_regular_file(
        root / "source-manifest.json", maximum_bytes=16 * 1024 * 1024
    )
    if (
        sha256(archive).hexdigest() != source.identity.archive_sha256
        or sha256(commit).hexdigest() != source.identity.commit_object_sha256
        or manifest
        != _canonical_json_bytes(source.manifest.model_dump(mode="json")) + b"\n"
    ):
        raise SandboxContractError(SandboxFailureReason.SOURCE_IDENTITY_MISMATCH)


def _runtime_source_binding(source: ReviewedSourceIdentity) -> OfflineBundleSourceBinding:
    return OfflineBundleSourceBinding(
        contract_version=source.contract_version,
        commit_id=source.commit_id,
        tree_hash=source.tree_hash,
        source_input_identity=source.source_input_identity,
        source_manifest_identity=source.source_manifest_identity,
        source_archive_sha256=source.archive_sha256,
    )


def _validate_runtime_copy(
    root: Path,
    *,
    source: ReviewedSourceIdentity,
    expected_contract: RuntimeBundleInputContract,
    expected_bundle_identity: str,
) -> None:
    try:
        manifest = verify_offline_runtime_bundle(
            root,
            expected_source=_runtime_source_binding(source),
        )
        manifest_bytes = (root / BUNDLE_MANIFEST_FILENAME).read_bytes()
        actual_contract = build_sealed_runtime_bundle_input(
            manifest,
            manifest_sha256=sha256(manifest_bytes).hexdigest(),
        )
    except (OSError, OfflineBundleContractError, ValueError) as exc:
        raise SandboxContractError(SandboxFailureReason.RUNTIME_BUNDLE_NOT_READY) from exc
    if actual_contract != expected_contract or manifest.bundle_identity != expected_bundle_identity:
        raise SandboxContractError(SandboxFailureReason.RUNTIME_BUNDLE_NOT_READY)


def _mapping_bindings(
    *,
    paths: BoundaryQualificationRunPaths,
    source: ReviewedSourceIdentity,
    runner_input: BoundaryRunnerManifest,
    control_identity: str,
    runtime_bundle: RuntimeBundleInputContract,
    qualification_run_identity: str,
    results_identity_namespace: str = "windows-sandbox-boundary-empty-results:v1",
) -> tuple[SandboxMappingBinding, ...]:
    return (
        SandboxMappingBinding(
            SandboxMappingRole.SOURCE,
            paths.source_input,
            _BOUNDARY_MAPPING_GUEST_PATHS[SandboxMappingRole.SOURCE],
            True,
            source.source_input_identity,
        ),
        SandboxMappingBinding(
            SandboxMappingRole.RUNNER,
            paths.runner_input,
            _BOUNDARY_MAPPING_GUEST_PATHS[SandboxMappingRole.RUNNER],
            True,
            runner_input.runner_input_identity,
        ),
        SandboxMappingBinding(
            SandboxMappingRole.CONTROL,
            paths.control_input,
            _BOUNDARY_MAPPING_GUEST_PATHS[SandboxMappingRole.CONTROL],
            True,
            control_identity,
        ),
        SandboxMappingBinding(
            SandboxMappingRole.RUNTIME_BUNDLE,
            paths.runtime_input,
            _BOUNDARY_MAPPING_GUEST_PATHS[SandboxMappingRole.RUNTIME_BUNDLE],
            True,
            runtime_bundle.runtime_bundle_contract_identity,
        ),
        SandboxMappingBinding(
            SandboxMappingRole.RESULTS_EXPORT,
            paths.results_export,
            _BOUNDARY_MAPPING_GUEST_PATHS[SandboxMappingRole.RESULTS_EXPORT],
            False,
            _identity(
                results_identity_namespace,
                qualification_run_identity,
            ),
            fresh_empty=True,
        ),
    )


class WindowsSandboxBoundaryQualificationRunnerService(WindowsSandboxRunnerService):
    """ISO-03 mode layered on the reviewed ISO-01 runner/platform seam."""

    def __init__(self, *, platform: SandboxPlatform | None = None) -> None:
        super().__init__(platform=platform or WindowsSandboxPlatform())

    def construct(
        self,
        request: BoundaryQualificationConstructionRequest,
    ) -> BoundaryQualificationRunContract:
        if re.fullmatch(r"[a-z0-9][a-z0-9._-]{7,63}", request.run_id) is None:
            raise SandboxContractError(SandboxFailureReason.MAPPING_CONTRACT_INVALID)
        platform_report = self._platform.probe()
        if (
            platform_report.state is not SandboxPlatformState.AVAILABLE
            or platform_report.report_identity
            != request.platform_qualification.platform_report_identity
            or request.platform_qualification.state
            is not WindowsHostQualificationState.QUALIFIED
            or not platform_report.protected_client_supported
        ):
            raise SandboxContractError(SandboxFailureReason.PLATFORM_UNAVAILABLE)
        protected = build_protected_root_manifest(request.protected_manifest.bindings)
        if protected.contract != request.protected_manifest.contract:
            raise SandboxContractError(
                SandboxFailureReason.PROTECTED_ROOT_MANIFEST_INCOMPLETE
            )
        if (
            request.host_before_inventory.protected_root_manifest_identity
            != protected.manifest_identity
            or request.host_before_inventory.immutable_verifier_identity
            != request.immutable_verifier_identity
        ):
            raise SandboxContractError(SandboxFailureReason.RESULT_IDENTITY_MISMATCH)
        validate_adversarial_probe_targets(
            request.probe_targets,
            manifest=protected,
        )

        implementation_repository = _resolve_safe_directory(
            request.implementation_repository_root,
            reason=SandboxFailureReason.SOURCE_REPOSITORY_INVALID,
        )
        reviewed_source_repository = _resolve_safe_directory(
            request.reviewed_source_repository_root,
            reason=SandboxFailureReason.SOURCE_REPOSITORY_INVALID,
        )
        protected_state_repository = _resolve_safe_directory(
            request.protected_state_repository_root,
            reason=SandboxFailureReason.MAPPING_CONTRACT_INVALID,
        )
        if (
            implementation_repository == reviewed_source_repository
            or protected_state_repository == reviewed_source_repository
        ):
            raise SandboxContractError(SandboxFailureReason.SOURCE_REPOSITORY_INVALID)
        runs_root = _resolve_safe_directory(
            request.runs_root,
            reason=SandboxFailureReason.MAPPING_CONTRACT_INVALID,
        )
        forbidden_roots = (
            implementation_repository,
            reviewed_source_repository,
            protected_state_repository,
            _resolve_safe_directory(
                request.user_profile,
                reason=SandboxFailureReason.MAPPING_CONTRACT_INVALID,
            ),
            _resolve_safe_directory(
                request.host_temp,
                reason=SandboxFailureReason.MAPPING_CONTRACT_INVALID,
            ),
            _resolve_safe_directory(
                request.host_venv,
                reason=SandboxFailureReason.MAPPING_CONTRACT_INVALID,
            ),
        )
        if any(_paths_overlap(runs_root, item) for item in forbidden_roots) or any(
            _paths_overlap(runs_root, item.path) for item in protected.bindings
        ):
            raise SandboxContractError(SandboxFailureReason.FORBIDDEN_HOST_MAPPING)
        try:
            run_root = runs_root / request.run_id
            run_root.mkdir(exist_ok=False)
            input_root = run_root / "input"
            input_root.mkdir()
            source_root = input_root / "source"
            runner_root = input_root / "runner"
            control_root = input_root / "control"
            runtime_root = input_root / "runtime"
            results_root = run_root / "results-export"
            audit_root = run_root / "host-audit"
            control_root.mkdir()
            results_root.mkdir()
            audit_root.mkdir()
        except OSError as exc:
            raise SandboxContractError(SandboxFailureReason.RESULTS_EXPORT_NOT_FRESH) from exc

        source = stage_reviewed_source(reviewed_source_repository, source_root)
        runner_input = stage_boundary_runner_input(
            implementation_repository,
            runner_root,
        )
        if (
            runner_input.implementation_base_commit_id
            != request.implementation_base_commit_id
            or runner_input.candidate_tree_hash != request.candidate_tree_hash
        ):
            raise SandboxContractError(SandboxFailureReason.FORBIDDEN_INPUT)
        if (
            request.host_harness.implementation_base_commit_id
            != request.implementation_base_commit_id
            or request.host_harness.candidate_tree_hash
            != request.candidate_tree_hash
        ):
            raise SandboxContractError(SandboxFailureReason.FORBIDDEN_INPUT)
        runtime_bundle, runtime_bundle_identity = copy_verified_runtime_bundle(
            request.runtime_bundle_root,
            runtime_root,
            source=source.identity,
        )
        layout = build_guest_layout()
        environment = build_environment_policy(
            run_live_environment=source.identity.run_live_environment,
            layout=layout,
        )
        qualification_run_identity = _identity(
            "iso03-boundary-qualification-run:v1",
            {
                "run_id": request.run_id,
                "source_input_identity": source.identity.source_input_identity,
                "runtime_bundle_identity": runtime_bundle_identity,
                "runner_input_identity": runner_input.runner_input_identity,
                "implementation_base_commit_id": (
                    runner_input.implementation_base_commit_id
                ),
                "candidate_tree_hash": runner_input.candidate_tree_hash,
                "host_harness_manifest_identity": (
                    request.host_harness.manifest_identity
                ),
                "protected_root_manifest_identity": protected.manifest_identity,
                "protected_writer_quiescence_contract_identity": (
                    request.protected_writer_quiescence.contract_identity
                ),
                "platform_qualification_identity": (
                    request.platform_qualification.qualification_identity
                ),
                "host_before_inventory_identity": (
                    request.host_before_inventory.inventory_identity
                ),
            },
        )
        control_base = QualificationControlBase(
            run_id=request.run_id,
            qualification_run_identity=qualification_run_identity,
            source=source.identity,
            runtime_bundle=runtime_bundle,
            runtime_bundle_identity=runtime_bundle_identity,
            runner_input=runner_input,
            host_harness=request.host_harness,
            protected_roots=protected.contract,
            probe_targets=request.probe_targets,
            platform_qualification=request.platform_qualification,
            immutable_verifier_identity=request.immutable_verifier_identity,
            host_before_inventory_identity=request.host_before_inventory.inventory_identity,
            protected_writer_quiescence_contract_identity=(
                request.protected_writer_quiescence.contract_identity
            ),
            guest_layout=layout,
            environment=environment,
            result_schema_identity=BOUNDARY_QUALIFICATION_RESULT_SCHEMA_IDENTITY,
        )
        control_identity = _identity(
            "windows-sandbox-qualification-control:v1",
            control_base.model_dump(mode="json"),
        )
        paths = BoundaryQualificationRunPaths(
            run_root=run_root,
            source_input=source_root,
            runner_input=runner_root,
            control_input=control_root,
            runtime_input=runtime_root,
            results_export=results_root,
            audit_root=audit_root,
            config_path=run_root / "sandbox.wsb",
        )
        bindings = _mapping_bindings(
            paths=paths,
            source=source.identity,
            runner_input=runner_input,
            control_identity=control_identity,
            runtime_bundle=runtime_bundle,
            qualification_run_identity=qualification_run_identity,
        )
        mapping_manifest = build_mapping_manifest(
            bindings,
            protected_manifest=protected,
            forbidden_host_roots=forbidden_roots,
        )
        configuration = render_wsb_configuration(
            mapping_manifest,
            protected_client_supported=platform_report.protected_client_supported,
        )
        guest_workspace_identity = _identity(
            "windows-sandbox-guest-workspace:v1",
            {
                "source_input_identity": source.identity.source_input_identity,
                "layout_identity": layout.layout_identity,
                "environment_policy_identity": environment.policy_identity,
                "execution_mode": SandboxExecutionMode.BOUNDARY_QUALIFICATION.value,
            },
        )
        runner_identity = build_runner_identity(
            RunnerIdentityMaterial(
                protected_root_manifest_identity=protected.manifest_identity,
                source_input_identity=source.identity.source_input_identity,
                wsb_config_identity=configuration.config_identity,
                mapping_manifest_identity=mapping_manifest.mapping_manifest_identity,
                guest_layout_identity=layout.layout_identity,
                environment_policy_identity=environment.policy_identity,
                result_schema_identity=BOUNDARY_QUALIFICATION_RESULT_SCHEMA_IDENTITY,
                runtime_bundle_contract_identity=(
                    runtime_bundle.runtime_bundle_contract_identity
                ),
                runner_bootstrap_identity=runner_input.runner_input_identity,
                control_inventory_identity=control_identity,
            )
        )
        host_acceptance_module = next(
            (
                item
                for item in request.host_harness.files
                if item.relative_path
                == "tradingagents/windows_sandbox_qualification.py"
            ),
            None,
        )
        if host_acceptance_module is None:
            raise SandboxContractError(SandboxFailureReason.FORBIDDEN_INPUT)
        acceptance_runner_structure = qualify_ticket06_acceptance_runner_structure(
            runner_root,
            expected_runner_identity=runner_input.runner_input_identity,
            base_environment_variables=environment.variables,
            mapping_manifest=mapping_manifest,
            wsb_configuration=configuration,
            boundary_runner_identity=runner_identity,
            host_harness_manifest_identity=request.host_harness.manifest_identity,
            host_acceptance_module_path=Path(__file__),
            expected_host_acceptance_module_sha256=(
                host_acceptance_module.sha256
            ),
        )
        assert runtime_bundle.bundle_manifest_sha256 is not None
        static = QualificationStaticBindings(
            qualification_run_identity=qualification_run_identity,
            source_commit_id=source.identity.commit_id,
            source_tree_hash=source.identity.tree_hash,
            source_input_identity=source.identity.source_input_identity,
            source_manifest_identity=source.identity.source_manifest_identity,
            source_archive_sha256=source.identity.archive_sha256,
            runtime_bundle_identity=runtime_bundle_identity,
            runtime_bundle_manifest_sha256=runtime_bundle.bundle_manifest_sha256,
            runtime_bundle_contract_identity=(
                runtime_bundle.runtime_bundle_contract_identity
            ),
            runner_identity=runner_identity,
            wsb_config_identity=configuration.config_identity,
            protected_root_manifest_identity=protected.manifest_identity,
            mapping_manifest_identity=mapping_manifest.mapping_manifest_identity,
            guest_workspace_identity=guest_workspace_identity,
            qualification_control_identity=control_identity,
            platform_qualification_identity=(
                request.platform_qualification.qualification_identity
            ),
            immutable_verifier_identity=request.immutable_verifier_identity,
            host_before_inventory_identity=request.host_before_inventory.inventory_identity,
            acceptance_runner_structure=acceptance_runner_structure,
        )
        envelope = BoundaryQualificationControlEnvelope(
            base=control_base,
            qualification_control_identity=control_identity,
            source=_runtime_source_binding(source.identity),
            runtime_bundle=runtime_bundle,
            derived=static,
        )
        try:
            (control_root / "run-control.json").write_bytes(
                _canonical_json_bytes(envelope.model_dump(mode="json")) + b"\n"
            )
            paths.config_path.write_text(
                configuration.xml,
                encoding="utf-8",
                newline="\n",
            )
        except OSError as exc:
            raise SandboxContractError(SandboxFailureReason.MAPPING_CONTRACT_INVALID) from exc
        contract = BoundaryQualificationRunContract(
            platform_report=platform_report,
            platform_qualification=request.platform_qualification,
            paths=paths,
            source=source,
            runtime_bundle=runtime_bundle,
            runtime_bundle_identity=runtime_bundle_identity,
            runner_input=runner_input,
            host_harness=request.host_harness,
            implementation_repository_root=implementation_repository,
            host_entrypoint_path=request.host_entrypoint_path,
            protected_manifest=protected,
            mapping_manifest=mapping_manifest,
            wsb_configuration=configuration,
            guest_layout=layout,
            environment_policy=environment,
            control=envelope,
            static_bindings=static,
            probe_targets=request.probe_targets,
            forbidden_host_roots=forbidden_roots,
        )
        self._revalidate_boundary_contract(contract)
        return contract

    def construct_ticket06_acceptance(
        self,
        request: Ticket06AcceptanceConstructionRequest,
    ) -> Ticket06AcceptanceRunContract:
        """Construct the second closed mode through the same qualified owner."""

        machine_identity_pattern = (
            r"^[a-z0-9][a-z0-9._-]*(?::[a-z0-9][a-z0-9._-]*)*:"
            r"sha256:[0-9a-f]{64}$"
        )
        if any(
            re.fullmatch(machine_identity_pattern, value) is None
            for value in (
                request.boundary_qualification_identity,
                request.frozen_baseline_identity,
            )
        ):
            raise SandboxContractError(SandboxFailureReason.FORBIDDEN_INPUT)

        boundary = self.construct(request.qualified_runner_request)
        source = Ticket06ControlSource.model_validate(
            boundary.source.identity.model_dump(mode="json")
        )
        runtime = Ticket06ControlRuntime.model_validate(
            boundary.runtime_bundle.model_dump(mode="json")
        )
        layout = Ticket06ControlGuestLayout.model_validate(
            boundary.guest_layout.model_dump(mode="json")
        )
        environment = build_ticket06_control_environment(
            boundary.environment_policy.variables
        )
        ledger = build_ticket06_command_ledger(environment.policy_identity)
        control_base = Ticket06AcceptanceControlBase(
            run_id=request.qualified_runner_request.run_id,
            source=source,
            runtime_bundle=runtime,
            runtime_bundle_identity=boundary.runtime_bundle_identity,
            runner_input_identity=boundary.runner_input.runner_input_identity,
            protected_root_manifest_identity=(
                boundary.protected_manifest.manifest_identity
            ),
            boundary_qualification_identity=request.boundary_qualification_identity,
            frozen_baseline_identity=request.frozen_baseline_identity,
            guest_layout=layout,
            environment=environment,
            command_ledger=ledger,
            result_schema_identity=TICKET06_ACCEPTANCE_RESULT_SCHEMA_IDENTITY,
        )
        control_identity = acceptance_content_identity(
            "windows-sandbox-ticket06-control:v1",
            control_base.model_dump(mode="json"),
        )
        acceptance_run_identity = _identity(
            "ticket06-sandbox-acceptance-run:v1",
            {
                "run_id": request.qualified_runner_request.run_id,
                "source_input_identity": source.source_input_identity,
                "runtime_bundle_identity": boundary.runtime_bundle_identity,
                "runner_input_identity": boundary.runner_input.runner_input_identity,
                "protected_root_manifest_identity": (
                    boundary.protected_manifest.manifest_identity
                ),
                "boundary_qualification_identity": (
                    request.boundary_qualification_identity
                ),
                "frozen_baseline_identity": request.frozen_baseline_identity,
                "command_ledger_identity": ledger.ledger_identity,
            },
        )
        bindings = _mapping_bindings(
            paths=boundary.paths,
            source=boundary.source.identity,
            runner_input=boundary.runner_input,
            control_identity=control_identity,
            runtime_bundle=boundary.runtime_bundle,
            qualification_run_identity=acceptance_run_identity,
            results_identity_namespace=(
                "windows-sandbox-ticket06-empty-results:v1"
            ),
        )
        mapping = build_mapping_manifest(
            bindings,
            protected_manifest=boundary.protected_manifest,
            forbidden_host_roots=boundary.forbidden_host_roots,
        )
        configuration = render_wsb_configuration(
            mapping,
            protected_client_supported=(
                boundary.platform_report.protected_client_supported
            ),
        )
        guest_workspace_identity = _identity(
            "windows-sandbox-guest-workspace:v1",
            {
                "source_input_identity": source.source_input_identity,
                "layout_identity": layout.layout_identity,
                "environment_policy_identity": environment.policy_identity,
                "execution_mode": SandboxExecutionMode.TICKET06_ACCEPTANCE.value,
            },
        )
        runner_identity = build_runner_identity(
            RunnerIdentityMaterial(
                protected_root_manifest_identity=(
                    boundary.protected_manifest.manifest_identity
                ),
                source_input_identity=source.source_input_identity,
                wsb_config_identity=configuration.config_identity,
                mapping_manifest_identity=mapping.mapping_manifest_identity,
                guest_layout_identity=layout.layout_identity,
                environment_policy_identity=environment.policy_identity,
                result_schema_identity=TICKET06_ACCEPTANCE_RESULT_SCHEMA_IDENTITY,
                runtime_bundle_contract_identity=(
                    runtime.runtime_bundle_contract_identity
                ),
                runner_bootstrap_identity=(
                    boundary.runner_input.runner_input_identity
                ),
                control_inventory_identity=control_identity,
            )
        )
        control = build_ticket06_acceptance_control_envelope(
            base=control_base,
            runner_identity=runner_identity,
            wsb_config_identity=configuration.config_identity,
            mapping_manifest_identity=mapping.mapping_manifest_identity,
            guest_workspace_identity=guest_workspace_identity,
        )
        try:
            (boundary.paths.control_input / "run-control.json").write_bytes(
                _canonical_json_bytes(control.model_dump(mode="json")) + b"\n"
            )
            boundary.paths.config_path.write_text(
                configuration.xml,
                encoding="utf-8",
                newline="\n",
            )
        except OSError as exc:
            raise SandboxContractError(
                SandboxFailureReason.MAPPING_CONTRACT_INVALID
            ) from exc
        contract = Ticket06AcceptanceRunContract(
            platform_report=boundary.platform_report,
            paths=boundary.paths,
            source=boundary.source,
            runtime_bundle=boundary.runtime_bundle,
            runtime_bundle_identity=boundary.runtime_bundle_identity,
            runner_input=boundary.runner_input,
            host_harness=boundary.host_harness,
            implementation_repository_root=(
                boundary.implementation_repository_root
            ),
            host_entrypoint_path=boundary.host_entrypoint_path,
            protected_manifest=boundary.protected_manifest,
            mapping_manifest=mapping,
            wsb_configuration=configuration,
            guest_layout=boundary.guest_layout,
            environment_policy=environment,
            control=control,
            forbidden_host_roots=boundary.forbidden_host_roots,
        )
        self._revalidate_ticket06_contract(contract)
        return contract

    def _revalidate_ticket06_contract(
        self,
        contract: Ticket06AcceptanceRunContract,
    ) -> None:
        current_platform = self._platform.probe()
        if current_platform != contract.platform_report:
            raise SandboxContractError(SandboxFailureReason.PLATFORM_CONTRACT_INVALID)
        current_host_harness = build_reviewed_host_harness_manifest(
            contract.implementation_repository_root,
            entrypoint_path=contract.host_entrypoint_path,
        )
        if current_host_harness != contract.host_harness:
            raise SandboxContractError(SandboxFailureReason.FORBIDDEN_INPUT)
        protected = build_protected_root_manifest(contract.protected_manifest.bindings)
        if protected.contract != contract.protected_manifest.contract:
            raise SandboxContractError(
                SandboxFailureReason.PROTECTED_ROOT_MANIFEST_INCOMPLETE
            )
        expected_layout = build_guest_layout()
        expected_environment = build_ticket06_control_environment(
            build_environment_policy(
                run_live_environment=contract.source.identity.run_live_environment,
                layout=expected_layout,
            ).variables
        )
        if (
            contract.guest_layout != expected_layout
            or contract.control.base.guest_layout
            != Ticket06ControlGuestLayout.model_validate(
                expected_layout.model_dump(mode="json")
            )
            or contract.environment_policy != expected_environment
            or contract.control.base.environment != expected_environment
        ):
            raise SandboxContractError(
                SandboxFailureReason.ENVIRONMENT_POLICY_INVALID
            )
        _validate_staged_source(contract.source, contract.paths.source_input)
        validate_boundary_runner_input(
            contract.paths.runner_input,
            expected=contract.runner_input,
        )
        _validate_runtime_copy(
            contract.paths.runtime_input,
            source=contract.source.identity,
            expected_contract=contract.runtime_bundle,
            expected_bundle_identity=contract.runtime_bundle_identity,
        )
        try:
            control_files = tuple(contract.paths.control_input.iterdir())
            control_bytes = _read_stable_regular_file(
                contract.paths.control_input / "run-control.json",
                maximum_bytes=262_144,
            )
            parsed_control = Ticket06AcceptanceControlEnvelope.model_validate_json(
                control_bytes
            )
        except ValueError as exc:
            raise SandboxContractError(
                SandboxFailureReason.MAPPING_CONTRACT_INVALID
            ) from exc
        if (
            len(control_files) != 1
            or control_files[0].name != "run-control.json"
            or parsed_control != contract.control
            or control_bytes
            != _canonical_json_bytes(contract.control.model_dump(mode="json")) + b"\n"
        ):
            raise SandboxContractError(
                SandboxFailureReason.MAPPING_CONTRACT_INVALID
            )
        acceptance_run_identity = _identity(
            "ticket06-sandbox-acceptance-run:v1",
            {
                "run_id": contract.control.base.run_id,
                "source_input_identity": (
                    contract.source.identity.source_input_identity
                ),
                "runtime_bundle_identity": contract.runtime_bundle_identity,
                "runner_input_identity": contract.runner_input.runner_input_identity,
                "protected_root_manifest_identity": protected.manifest_identity,
                "boundary_qualification_identity": (
                    contract.control.base.boundary_qualification_identity
                ),
                "frozen_baseline_identity": (
                    contract.control.base.frozen_baseline_identity
                ),
                "command_ledger_identity": (
                    contract.control.base.command_ledger.ledger_identity
                ),
            },
        )
        bindings = _mapping_bindings(
            paths=contract.paths,
            source=contract.source.identity,
            runner_input=contract.runner_input,
            control_identity=contract.control.control_identity,
            runtime_bundle=contract.runtime_bundle,
            qualification_run_identity=acceptance_run_identity,
            results_identity_namespace=(
                "windows-sandbox-ticket06-empty-results:v1"
            ),
        )
        mapping = build_mapping_manifest(
            bindings,
            protected_manifest=protected,
            forbidden_host_roots=contract.forbidden_host_roots,
        )
        if mapping.contract != contract.mapping_manifest.contract:
            raise SandboxContractError(SandboxFailureReason.MAPPING_CONTRACT_INVALID)
        configuration = render_wsb_configuration(
            mapping,
            protected_client_supported=current_platform.protected_client_supported,
        )
        validate_wsb_configuration(
            configuration.xml,
            mapping_manifest=mapping,
            protected_client_supported=current_platform.protected_client_supported,
        )
        config_bytes = _read_stable_regular_file(
            contract.paths.config_path,
            maximum_bytes=262_144,
        )
        if (
            configuration != contract.wsb_configuration
            or config_bytes != configuration.xml.encode("utf-8")
            or any(contract.paths.results_export.iterdir())
        ):
            raise SandboxContractError(SandboxFailureReason.PLATFORM_CONTRACT_INVALID)
        expected_runner = build_runner_identity(
            RunnerIdentityMaterial(
                protected_root_manifest_identity=protected.manifest_identity,
                source_input_identity=contract.source.identity.source_input_identity,
                wsb_config_identity=configuration.config_identity,
                mapping_manifest_identity=mapping.mapping_manifest_identity,
                guest_layout_identity=contract.guest_layout.layout_identity,
                environment_policy_identity=contract.environment_policy.policy_identity,
                result_schema_identity=TICKET06_ACCEPTANCE_RESULT_SCHEMA_IDENTITY,
                runtime_bundle_contract_identity=(
                    contract.runtime_bundle.runtime_bundle_contract_identity
                ),
                runner_bootstrap_identity=(
                    contract.runner_input.runner_input_identity
                ),
                control_inventory_identity=contract.control.control_identity,
            )
        )
        if expected_runner != contract.control.derived.runner_identity:
            raise SandboxContractError(SandboxFailureReason.RESULT_IDENTITY_MISMATCH)

    def _revalidate_boundary_contract(
        self,
        contract: BoundaryQualificationRunContract,
    ) -> None:
        current_platform = self._platform.probe()
        if current_platform != contract.platform_report:
            raise SandboxContractError(SandboxFailureReason.PLATFORM_CONTRACT_INVALID)
        current_host_harness = build_reviewed_host_harness_manifest(
            contract.implementation_repository_root,
            entrypoint_path=contract.host_entrypoint_path,
        )
        if current_host_harness != contract.host_harness:
            raise SandboxContractError(SandboxFailureReason.FORBIDDEN_INPUT)
        protected = build_protected_root_manifest(contract.protected_manifest.bindings)
        if protected.contract != contract.protected_manifest.contract:
            raise SandboxContractError(
                SandboxFailureReason.PROTECTED_ROOT_MANIFEST_INCOMPLETE
            )
        validate_adversarial_probe_targets(
            contract.probe_targets,
            manifest=protected,
        )
        _validate_staged_source(contract.source, contract.paths.source_input)
        validate_boundary_runner_input(
            contract.paths.runner_input,
            expected=contract.runner_input,
        )
        _validate_runtime_copy(
            contract.paths.runtime_input,
            source=contract.source.identity,
            expected_contract=contract.runtime_bundle,
            expected_bundle_identity=contract.runtime_bundle_identity,
        )
        try:
            control_files = tuple(contract.paths.control_input.iterdir())
            control_bytes = _read_stable_regular_file(
                contract.paths.control_input / "run-control.json",
                maximum_bytes=262_144,
            )
            parsed_control = BoundaryQualificationControlEnvelope.model_validate_json(
                control_bytes
            )
        except ValueError as exc:
            raise SandboxContractError(SandboxFailureReason.MAPPING_CONTRACT_INVALID) from exc
        if (
            len(control_files) != 1
            or control_files[0].name != "run-control.json"
            or parsed_control != contract.control
            or control_bytes
            != _canonical_json_bytes(contract.control.model_dump(mode="json")) + b"\n"
        ):
            raise SandboxContractError(SandboxFailureReason.MAPPING_CONTRACT_INVALID)
        bindings = _mapping_bindings(
            paths=contract.paths,
            source=contract.source.identity,
            runner_input=contract.runner_input,
            control_identity=contract.control.qualification_control_identity,
            runtime_bundle=contract.runtime_bundle,
            qualification_run_identity=(
                contract.static_bindings.qualification_run_identity
            ),
        )
        mapping = build_mapping_manifest(
            bindings,
            protected_manifest=protected,
            forbidden_host_roots=contract.forbidden_host_roots,
        )
        if mapping.contract != contract.mapping_manifest.contract:
            raise SandboxContractError(SandboxFailureReason.MAPPING_CONTRACT_INVALID)
        configuration = render_wsb_configuration(
            mapping,
            protected_client_supported=current_platform.protected_client_supported,
        )
        validate_wsb_configuration(
            configuration.xml,
            mapping_manifest=mapping,
            protected_client_supported=current_platform.protected_client_supported,
        )
        try:
            config_bytes = _read_stable_regular_file(
                contract.paths.config_path,
                maximum_bytes=262_144,
            )
        except SandboxContractError as exc:
            raise SandboxContractError(SandboxFailureReason.PLATFORM_CONTRACT_INVALID) from exc
        if (
            configuration != contract.wsb_configuration
            or config_bytes != configuration.xml.encode("utf-8")
            or any(contract.paths.results_export.iterdir())
        ):
            raise SandboxContractError(SandboxFailureReason.PLATFORM_CONTRACT_INVALID)
        expected_runner_identity = build_runner_identity(
            RunnerIdentityMaterial(
                protected_root_manifest_identity=protected.manifest_identity,
                source_input_identity=contract.source.identity.source_input_identity,
                wsb_config_identity=configuration.config_identity,
                mapping_manifest_identity=mapping.mapping_manifest_identity,
                guest_layout_identity=contract.guest_layout.layout_identity,
                environment_policy_identity=contract.environment_policy.policy_identity,
                result_schema_identity=BOUNDARY_QUALIFICATION_RESULT_SCHEMA_IDENTITY,
                runtime_bundle_contract_identity=(
                    contract.runtime_bundle.runtime_bundle_contract_identity
                ),
                runner_bootstrap_identity=contract.runner_input.runner_input_identity,
                control_inventory_identity=(
                    contract.control.qualification_control_identity
                ),
            )
        )
        host_acceptance_module = next(
            (
                item
                for item in current_host_harness.files
                if item.relative_path
                == "tradingagents/windows_sandbox_qualification.py"
            ),
            None,
        )
        if host_acceptance_module is None:
            raise SandboxContractError(SandboxFailureReason.FORBIDDEN_INPUT)
        acceptance_runner_structure = qualify_ticket06_acceptance_runner_structure(
            contract.paths.runner_input,
            expected_runner_identity=contract.runner_input.runner_input_identity,
            base_environment_variables=contract.environment_policy.variables,
            mapping_manifest=mapping,
            wsb_configuration=configuration,
            boundary_runner_identity=expected_runner_identity,
            host_harness_manifest_identity=current_host_harness.manifest_identity,
            host_acceptance_module_path=Path(__file__),
            expected_host_acceptance_module_sha256=(
                host_acceptance_module.sha256
            ),
        )
        if (
            expected_runner_identity != contract.static_bindings.runner_identity
            or acceptance_runner_structure
            != contract.static_bindings.acceptance_runner_structure
            or contract.control.derived != contract.static_bindings
        ):
            raise SandboxContractError(SandboxFailureReason.RESULT_IDENTITY_MISMATCH)

    def launch_boundary_qualification(
        self,
        contract: BoundaryQualificationRunContract,
        *,
        timeout_seconds: int,
    ) -> BoundaryQualificationExecution:
        process = self.launch_boundary_process(
            contract,
            timeout_seconds=timeout_seconds,
        )
        if process.timed_out or process.returncode != 0:
            raise SandboxContractError(SandboxFailureReason.SANDBOX_LAUNCH_FAILED)
        exported = self.validate_boundary_result(contract)
        return BoundaryQualificationExecution(process=process, export=exported)

    def launch_boundary_process(
        self,
        contract: BoundaryQualificationRunContract,
        *,
        timeout_seconds: int,
    ) -> SandboxProcessResult:
        self._revalidate_boundary_contract(contract)
        process = self._platform.launch(
            contract.paths.config_path,
            timeout_seconds=timeout_seconds,
        )
        return process

    @staticmethod
    def validate_boundary_result(
        contract: BoundaryQualificationRunContract,
    ) -> ValidatedBoundaryQualificationExport:
        return validate_boundary_qualification_export(
            contract.paths.results_export,
            expected=contract.static_bindings,
            expected_probe_targets=contract.probe_targets,
        )

    def launch_ticket06_acceptance(
        self,
        contract: Ticket06AcceptanceRunContract,
        *,
        timeout_seconds: int,
    ) -> Ticket06AcceptanceExecution:
        """Launch one acceptance guest and retain a bounded PASS or FAIL result."""

        self._revalidate_ticket06_contract(contract)
        observed_process = self._platform.launch(
            contract.paths.config_path,
            timeout_seconds=timeout_seconds,
        )
        process_evidence = persist_iso04_sandbox_process_evidence(
            audit_root=contract.paths.audit_root,
            run_id=contract.control.base.run_id,
            acceptance_control_identity=contract.control.control_identity,
            process=observed_process,
        )
        process = process_evidence.to_process_result()
        if (
            process.timed_out
            or process.returncode != 0
            or not process.termination_confirmed
        ):
            raise SandboxContractError(SandboxFailureReason.SANDBOX_LAUNCH_FAILED)
        exported = validate_ticket06_acceptance_export(
            contract.paths.results_export,
            expected=contract.control.derived,
        )
        return Ticket06AcceptanceExecution(
            sandbox_process_evidence=process_evidence,
            export=exported,
        )


class ISO03QualificationStatus(str, Enum):
    PASS = "pass"
    FAIL = "fail"
    NEEDS_INFO = "needs-info"


ISO03_PRECONSTRUCTION_FAILURE_RECEIPT_PREFIX = (
    "iso03-preconstruction-failure-"
)


class ISO03PreconstructionFailureReceipt(BaseModel):
    """Durable facts available when ISO-03 fails before HOST BEFORE."""

    model_config = _CLOSED_MODEL_CONFIG

    contract_version: Literal[
        "iso03-preconstruction-failure-receipt-v1"
    ] = "iso03-preconstruction-failure-receipt-v1"
    run_id: str = Field(pattern=r"^[a-z0-9][a-z0-9._-]{7,63}$")
    stage: Literal["historical_evidence_validation"] = (
        "historical_evidence_validation"
    )
    failure_reason: SandboxFailureReason
    implementation_repository_path_identity: str = Field(
        pattern=r"^host-path:sha256:[0-9a-f]{64}$"
    )
    reviewed_source_repository_path_identity: str = Field(
        pattern=r"^host-path:sha256:[0-9a-f]{64}$"
    )
    runtime_bundle_root_path_identity: str = Field(
        pattern=r"^host-path:sha256:[0-9a-f]{64}$"
    )
    platform_qualification_identity: str = Field(
        pattern=(
            r"^windows-sandbox-host-platform-qualification:v1:sha256:"
            r"[0-9a-f]{64}$"
        )
    )
    minimal_launch_evidence_identity: str = Field(min_length=1, max_length=180)
    minimal_sandbox_launch_succeeded: bool
    minimal_sandbox_process_ids: tuple[int, ...]
    implementation_base_commit_id: str = Field(pattern=r"^[0-9a-f]{40}$")
    candidate_tree_hash: str = Field(pattern=r"^[0-9a-f]{40}$")
    host_harness_manifest_identity: str = Field(
        pattern=r"^iso03-reviewed-host-harness:v1:sha256:[0-9a-f]{64}$"
    )
    protected_root_manifest_identity: str = Field(
        pattern=r"^windows-sandbox-protected-roots:v[1-3]:sha256:[0-9a-f]{64}$"
    )
    protected_writer_quiescence_contract_identity: str = Field(
        pattern=(
            r"^windows-sandbox-protected-writer-quiescence:v1:sha256:"
            r"[0-9a-f]{64}$"
        )
    )
    reviewed_source_input_identity: None = None
    runtime_bundle_identity: None = None
    runner_input_identity: None = None
    host_before_inventory_identity: None = None
    host_before_inventory_materialized: Literal[False] = False
    boundary_sandbox_constructed: Literal[False] = False
    boundary_sandbox_launched: Literal[False] = False
    receipt_identity: str = Field(
        pattern=(
            r"^iso03-preconstruction-failure-receipt:v1:sha256:"
            r"[0-9a-f]{64}$"
        )
    )

    @model_validator(mode="after")
    def _validate_receipt(self) -> ISO03PreconstructionFailureReceipt:
        if self.minimal_sandbox_launch_succeeded != bool(
            self.minimal_sandbox_process_ids
        ):
            raise ValueError("minimal Sandbox status is inconsistent")
        payload = self.model_dump(mode="json", exclude={"receipt_identity"})
        if self.receipt_identity != _identity(
            "iso03-preconstruction-failure-receipt:v1",
            payload,
        ):
            raise ValueError("preconstruction failure receipt identity mismatch")
        return self

    @classmethod
    def create(
        cls,
        *,
        run_id: str,
        failure_reason: SandboxFailureReason,
        implementation_repository: Path,
        reviewed_source_repository: Path,
        runtime_bundle_root: Path,
        platform_qualification: WindowsHostPlatformQualification,
        minimal_launch: MinimalSandboxLaunchEvidence,
        implementation_base_commit_id: str,
        candidate_tree_hash: str,
        host_harness: ReviewedHostHarnessManifest,
        protected_roots: ResolvedProtectedRootManifest,
        protected_writer_quiescence: ProtectedWriterQuiescenceContract,
    ) -> ISO03PreconstructionFailureReceipt:
        payload = {
            "contract_version": "iso03-preconstruction-failure-receipt-v1",
            "run_id": run_id,
            "stage": "historical_evidence_validation",
            "failure_reason": failure_reason.value,
            "implementation_repository_path_identity": host_path_identity(
                implementation_repository
            ),
            "reviewed_source_repository_path_identity": host_path_identity(
                reviewed_source_repository
            ),
            "runtime_bundle_root_path_identity": host_path_identity(
                runtime_bundle_root
            ),
            "platform_qualification_identity": (
                platform_qualification.qualification_identity
            ),
            "minimal_launch_evidence_identity": minimal_launch.evidence_identity,
            "minimal_sandbox_launch_succeeded": minimal_launch.succeeded,
            "minimal_sandbox_process_ids": list(
                minimal_launch.launched_process_ids
            ),
            "implementation_base_commit_id": implementation_base_commit_id,
            "candidate_tree_hash": candidate_tree_hash,
            "host_harness_manifest_identity": host_harness.manifest_identity,
            "protected_root_manifest_identity": protected_roots.manifest_identity,
            "protected_writer_quiescence_contract_identity": (
                protected_writer_quiescence.contract_identity
            ),
            "reviewed_source_input_identity": None,
            "runtime_bundle_identity": None,
            "runner_input_identity": None,
            "host_before_inventory_identity": None,
            "host_before_inventory_materialized": False,
            "boundary_sandbox_constructed": False,
            "boundary_sandbox_launched": False,
        }
        return cls(
            **payload,
            receipt_identity=_identity(
                "iso03-preconstruction-failure-receipt:v1",
                payload,
            ),
        )


class ISO03HostQualificationReport(BaseModel):
    model_config = _CLOSED_MODEL_CONFIG

    contract_version: Literal["iso03-host-qualification-report-v1"] = (
        "iso03-host-qualification-report-v1"
    )
    status: ISO03QualificationStatus
    run_id: str = Field(pattern=r"^[a-z0-9][a-z0-9._-]{7,63}$")
    platform_report: SandboxPlatformReport
    platform_qualification: WindowsHostPlatformQualification
    static_bindings: QualificationStaticBindings | None
    runner_input_identity: str | None = Field(default=None, max_length=180)
    implementation_base_commit_id: str | None = Field(
        default=None, pattern=r"^[0-9a-f]{40}$"
    )
    candidate_tree_hash: str | None = Field(
        default=None, pattern=r"^[0-9a-f]{40}$"
    )
    host_harness: ReviewedHostHarnessManifest | None
    protected_writer_quiescence: ProtectedWriterQuiescenceEvidence | None
    actual_sandbox_launch: bool
    sandbox_returncode: int | None = Field(default=None, ge=-1, le=65535)
    sandbox_timed_out: bool
    sandbox_termination_confirmed: bool
    sandbox_process_ids: tuple[int, ...]
    sandbox_process_before: tuple[SandboxProcessTopologyEntry, ...]
    sandbox_process_observed: tuple[SandboxProcessTopologyEntry, ...]
    sandbox_process_after: tuple[SandboxProcessTopologyEntry, ...]
    guest_result: GuestQualificationResult | None
    guest_export_identity: str | None = Field(default=None, max_length=180)
    host_before_inventory_identity: str | None = Field(default=None, max_length=180)
    host_after_inventory_identity: str | None = Field(default=None, max_length=180)
    host_delta: HostInventoryDelta | None
    semantic_before: dict[str, object] | None
    semantic_after: dict[str, object] | None
    failure_reason: str | None = Field(default=None, max_length=120)
    ticket06_executed: Literal[False] = False
    ticket07_executed: Literal[False] = False
    provider_model_io: Literal[False] = False
    calendar_refresh_executed: Literal[False] = False
    baseline_frozen: Literal[False] = False
    qualified_v1_enabled: Literal[False] = False
    report_identity: str = Field(
        pattern=r"^iso03-host-qualification-report:v1:sha256:[0-9a-f]{64}$"
    )

    @model_validator(mode="after")
    def _validate_report(self) -> ISO03HostQualificationReport:
        if self.status is ISO03QualificationStatus.PASS and (
            self.platform_qualification.state
            is not WindowsHostQualificationState.QUALIFIED
            or not self.actual_sandbox_launch
            or self.sandbox_returncode != 0
            or self.sandbox_timed_out
            or not self.sandbox_termination_confirmed
            or self.sandbox_process_before
            or len(self.sandbox_process_observed) < 2
            or not any(
                item.image_name == "WindowsSandbox.exe"
                for item in self.sandbox_process_observed
            )
            or self.sandbox_process_after
            or self.sandbox_process_ids
            != tuple(item.process_id for item in self.sandbox_process_observed)
            or self.static_bindings is None
            or self.implementation_base_commit_id is None
            or self.candidate_tree_hash is None
            or self.host_harness is None
            or self.host_harness.implementation_base_commit_id
            != self.implementation_base_commit_id
            or self.host_harness.candidate_tree_hash != self.candidate_tree_hash
            or self.protected_writer_quiescence is None
            or self.protected_writer_quiescence.immediate_before_inventory_identity
            != self.host_before_inventory_identity
            or self.guest_result is None
            or self.guest_export_identity is None
            or self.host_delta is None
            or not self.host_delta.equal
            or self.host_before_inventory_identity
            != self.host_after_inventory_identity
            or self.semantic_before != self.semantic_after
            or self.failure_reason is not None
        ):
            raise ValueError("ISO-03 PASS report has an unmet qualification gate")
        payload = self.model_dump(mode="json", exclude={"report_identity"})
        if self.report_identity != _identity(
            "iso03-host-qualification-report:v1", payload
        ):
            raise ValueError("ISO-03 host report identity mismatch")
        return self


def _semantic_summary(semantic: dict[str, object]) -> dict[str, object]:
    provider = semantic["provider_ledger"]
    calendar = semantic["calendar"]
    assert isinstance(provider, dict)
    assert isinstance(calendar, dict)
    sequences = provider["sequences"]
    attempts = provider["attempts"]
    assert isinstance(sequences, dict)
    assert isinstance(attempts, dict)
    return {
        "provider_sequences": {
            "count": sequences["count"],
            "inventory_sha256": sequences["inventory_sha256"],
        },
        "provider_attempts": {
            "count": attempts["count"],
            "inventory_sha256": attempts["inventory_sha256"],
        },
        "incident_rows": provider["incident_rows"],
        "calendar_revisions": calendar["revisions"],
        "calendar_rows": {
            "count": calendar["calendar_rows"]["count"],
            "inventory_sha256": calendar["calendar_rows"]["inventory_sha256"],
        },
        "calendar_sessions": {
            "count": calendar["session_rows"]["count"],
            "inventory_sha256": calendar["session_rows"]["inventory_sha256"],
        },
        "checkpoints": semantic["checkpoints"],
        "reports": semantic["reports"],
        "ticket07_evidence": {
            key: semantic["ticket07_evidence"][key]
            for key in ("byte_length", "file_count", "inventory_sha256")
        },
        "run_live_environment": semantic["run_live_environment"],
    }


def _immutable_semantic_capture(
    manifest: ResolvedProtectedRootManifest,
    *,
    capture: Callable[..., dict[str, object]],
) -> dict[str, object]:
    by_role = {binding.role: binding.path for binding in manifest.bindings}
    run_live_environment = (
        "RUN_LIVE_DEEPSEEK_TESTS",
        "RUN_LIVE_PROVIDER_TESTS",
        "RUN_LIVE_TUSHARE_TESTS",
    )
    missing = object()
    previous: dict[str, str | object] = {
        name: os.environ.get(name, missing) for name in run_live_environment
    }
    try:
        for name in run_live_environment:
            os.environ[name] = "0"
        return capture(
            provider_ledger=by_role[ProtectedRootRole.PROVIDER_LEDGER],
            calendar_database=by_role[ProtectedRootRole.CALENDAR_DATABASE],
            ticket07_root=by_role[ProtectedRootRole.TICKET07_EVIDENCE],
            reports_root=by_role[ProtectedRootRole.REPORTS_LOGS],
            checkpoints_root=by_role[ProtectedRootRole.CHECKPOINTS],
            enforce_expected_counts=True,
        )
    finally:
        for name, value in previous.items():
            if value is missing:
                os.environ.pop(name, None)
            else:
                assert isinstance(value, str)
                os.environ[name] = value


def _load_immutable_semantic_capture(
    verifier_path: Path,
    verifier_bytes: bytes,
) -> Callable[..., dict[str, object]]:
    namespace: dict[str, object] = {
        "__file__": str(verifier_path),
        "__name__": "iso03_trusted_immutable_state_verifier",
        "__package__": None,
    }
    try:
        code = compile(verifier_bytes, str(verifier_path), "exec", dont_inherit=True)
        exec(code, namespace)
        capture = namespace["capture_immutable_state"]
    except (KeyError, SyntaxError, TypeError) as exc:
        raise SandboxContractError(SandboxFailureReason.PLATFORM_CONTRACT_INVALID) from exc
    if not callable(capture):
        raise SandboxContractError(SandboxFailureReason.PLATFORM_CONTRACT_INVALID)
    return capture


def _write_host_audit(path: Path, value: BaseModel | dict[str, object]) -> None:
    payload = value.model_dump(mode="json") if isinstance(value, BaseModel) else value
    data = _canonical_json_bytes(payload) + b"\n"
    try:
        with path.open("xb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
    except OSError as exc:
        raise SandboxContractError(SandboxFailureReason.UNSAFE_RESULT_EXPORT) from exc


def _build_iso03_report(**values: object) -> ISO03HostQualificationReport:
    model_values = {
        "contract_version": "iso03-host-qualification-report-v1",
        **values,
    }
    payload = {
        key: value.model_dump(mode="json")
        if isinstance(value, BaseModel)
        else value.value
        if isinstance(value, Enum)
        else [
            item.model_dump(mode="json") if isinstance(item, BaseModel) else item
            for item in value
        ]
        if isinstance(value, tuple)
        else value
        for key, value in model_values.items()
    }
    return ISO03HostQualificationReport(
        **model_values,
        report_identity=_identity("iso03-host-qualification-report:v1", payload),
    )


def execute_iso03_boundary_qualification(
    *,
    implementation_repository_root: Path,
    reviewed_source_repository_root: Path,
    protected_state_repository_root: Path,
    historical_qualification_runs_root: Path,
    replacement_qualification_run_root: Path,
    qualified_replacement_qualification_root: Path,
    failed_iso04_ticket06_acceptance_root: Path,
    review_invalidated_qualification_root: Path,
    runs_root: Path,
    runtime_bundle_root: Path,
    run_id: str,
    user_profile: Path,
    host_temp: Path,
    host_venv: Path,
    host_entrypoint_path: Path,
    minimal_timeout_seconds: int = 180,
    qualification_timeout_seconds: int = 1_800,
    protected_writers_quiesced: bool = False,
    platform: SandboxPlatform | None = None,
    host_fact_capture: Callable[[], WindowsHostPlatformFacts] = (
        capture_windows_host_platform_facts
    ),
) -> ISO03HostQualificationReport:
    """Execute only ISO-03 and make the trusted HOST comparison final authority."""

    actual_platform = platform or WindowsSandboxPlatform()
    platform_report = actual_platform.probe()
    try:
        facts = host_fact_capture()
    except (SandboxContractError, OSError, RuntimeError, ValueError):
        facts = _unavailable_windows_host_platform_facts()
        platform_report = SandboxPlatformReport.unavailable(
            state=SandboxPlatformState.NEEDS_INFO,
            diagnostic_code="host-platform-facts-unavailable",
        )
    if (
        platform_report.state is SandboxPlatformState.AVAILABLE
        and platform_report.protected_client_supported
    ):
        try:
            minimal = run_minimal_sandbox_launch(
                platform=actual_platform,
                evidence_root=runs_root,
                run_id=run_id,
                timeout_seconds=minimal_timeout_seconds,
            )
        except (SandboxContractError, OSError, RuntimeError, ValueError):
            platform_report = SandboxPlatformReport.unavailable(
                state=SandboxPlatformState.NEEDS_INFO,
                diagnostic_code="minimal-sandbox-launch-unavailable",
            )
            minimal = MinimalSandboxLaunchEvidence.create(
                succeeded=False,
                returncode=-1,
                timed_out=False,
                config_identity=_identity(
                    "windows-sandbox-minimal-config:v1", "launch-unavailable"
                ),
                marker_sha256=None,
            )
    else:
        minimal = MinimalSandboxLaunchEvidence.create(
            succeeded=False,
            returncode=-1,
            timed_out=False,
            config_identity=_identity("windows-sandbox-minimal-config:v1", "not-run"),
            marker_sha256=None,
        )
    platform_qualification = qualify_windows_sandbox_host(
        platform_report=platform_report,
        facts=facts,
        minimal_launch=minimal,
    )
    minimal_audit = runs_root / f"minimal-{run_id}" / "platform-qualification.json"
    if minimal_audit.parent.is_dir():
        _write_host_audit(minimal_audit, platform_qualification)
    if platform_qualification.state is not WindowsHostQualificationState.QUALIFIED:
        return _build_iso03_report(
            status=ISO03QualificationStatus.NEEDS_INFO,
            run_id=run_id,
            platform_report=platform_report,
            platform_qualification=platform_qualification,
            static_bindings=None,
            runner_input_identity=None,
            implementation_base_commit_id=None,
            candidate_tree_hash=None,
            host_harness=None,
            protected_writer_quiescence=None,
            actual_sandbox_launch=False,
            sandbox_returncode=None,
            sandbox_timed_out=False,
            sandbox_termination_confirmed=True,
            sandbox_process_ids=(),
            sandbox_process_before=(),
            sandbox_process_observed=(),
            sandbox_process_after=(),
            guest_result=None,
            guest_export_identity=None,
            host_before_inventory_identity=None,
            host_after_inventory_identity=None,
            host_delta=None,
            semantic_before=None,
            semantic_after=None,
            failure_reason="host_platform_not_qualified",
            ticket06_executed=False,
            ticket07_executed=False,
            provider_model_io=False,
            calendar_refresh_executed=False,
            baseline_frozen=False,
            qualified_v1_enabled=False,
        )

    try:
        quiescence_contract = build_protected_writer_quiescence_contract(
            operator_attested=protected_writers_quiesced,
        )
    except SandboxContractError:
        return _build_iso03_report(
            status=ISO03QualificationStatus.NEEDS_INFO,
            run_id=run_id,
            platform_report=platform_report,
            platform_qualification=platform_qualification,
            static_bindings=None,
            runner_input_identity=None,
            implementation_base_commit_id=None,
            candidate_tree_hash=None,
            host_harness=None,
            protected_writer_quiescence=None,
            actual_sandbox_launch=False,
            sandbox_returncode=None,
            sandbox_timed_out=False,
            sandbox_termination_confirmed=True,
            sandbox_process_ids=(),
            sandbox_process_before=(),
            sandbox_process_observed=(),
            sandbox_process_after=(),
            guest_result=None,
            guest_export_identity=None,
            host_before_inventory_identity=None,
            host_after_inventory_identity=None,
            host_delta=None,
            semantic_before=None,
            semantic_after=None,
            failure_reason="protected_writers_not_attested_quiescent",
            ticket06_executed=False,
            ticket07_executed=False,
            provider_model_io=False,
            calendar_refresh_executed=False,
            baseline_frozen=False,
            qualified_v1_enabled=False,
        )

    implementation_repository = _resolve_safe_directory(
        implementation_repository_root,
        reason=SandboxFailureReason.SOURCE_REPOSITORY_INVALID,
    )
    implementation_base_commit_id = _committed_git_hex(
        implementation_repository,
        "HEAD^{commit}",
    )
    candidate_tree_hash = _reviewed_index_tree_hash(implementation_repository)
    host_harness = build_reviewed_host_harness_manifest(
        implementation_repository,
        entrypoint_path=host_entrypoint_path,
    )
    if (
        host_harness.implementation_base_commit_id
        != implementation_base_commit_id
        or host_harness.candidate_tree_hash != candidate_tree_hash
    ):
        raise SandboxContractError(SandboxFailureReason.FORBIDDEN_INPUT)
    protected_state_repository = _resolve_safe_directory(
        protected_state_repository_root,
        reason=SandboxFailureReason.MAPPING_CONTRACT_INVALID,
    )
    protected = build_protected_root_manifest(
        default_protected_root_bindings(
            user_profile=user_profile,
            repository_root=protected_state_repository,
            historical_qualification_runs_root=(
                historical_qualification_runs_root
            ),
            replacement_qualification_run_root=(
                replacement_qualification_run_root
            ),
            qualified_replacement_qualification_root=(
                qualified_replacement_qualification_root
            ),
            failed_iso04_ticket06_acceptance_root=(
                failed_iso04_ticket06_acceptance_root
            ),
            review_invalidated_qualification_root=(
                review_invalidated_qualification_root
            ),
        )
    )
    try:
        validate_iso03_historical_evidence_root_coverage(protected)
    except SandboxContractError as exc:
        receipt = ISO03PreconstructionFailureReceipt.create(
            run_id=run_id,
            failure_reason=exc.reason,
            implementation_repository=implementation_repository,
            reviewed_source_repository=reviewed_source_repository_root,
            runtime_bundle_root=runtime_bundle_root,
            platform_qualification=platform_qualification,
            minimal_launch=minimal,
            implementation_base_commit_id=implementation_base_commit_id,
            candidate_tree_hash=candidate_tree_hash,
            host_harness=host_harness,
            protected_roots=protected,
            protected_writer_quiescence=quiescence_contract,
        )
        receipt_path = runs_root / (
            f"{ISO03_PRECONSTRUCTION_FAILURE_RECEIPT_PREFIX}{run_id}.json"
        )
        _write_host_audit(receipt_path, receipt)
        raise
    verifier_path = (
        implementation_repository
        / "scripts"
        / "verify_ticket06_immutable_state.py"
    )
    verifier_bytes = _read_committed_git_object(
        implementation_repository,
        (
            "show",
            ":scripts/verify_ticket06_immutable_state.py",
        ),
        maximum_bytes=1_048_576,
    )
    immutable_verifier_identity = _identity(
        "ticket06-immutable-state-verifier:v1",
        {
            "byte_length": len(verifier_bytes),
            "sha256": sha256(verifier_bytes).hexdigest(),
        },
    )
    semantic_capture = _load_immutable_semantic_capture(
        verifier_path,
        verifier_bytes,
    )
    sentinel = capture_protected_host_inventory(
        protected,
        semantic_capture=lambda: _immutable_semantic_capture(
            protected,
            capture=semantic_capture,
        ),
        immutable_verifier_identity=immutable_verifier_identity,
    )
    _validate_historical_report_pins_in_inventory(
        sentinel,
        _HISTORICAL_REPORT_PINS,
    )
    validate_verifier_root_ownership(sentinel)
    targets = select_adversarial_probe_targets(protected, run_id=run_id)
    service = WindowsSandboxBoundaryQualificationRunnerService(platform=actual_platform)
    contract = service.construct(
        BoundaryQualificationConstructionRequest(
            implementation_repository_root=implementation_repository_root,
            implementation_base_commit_id=implementation_base_commit_id,
            candidate_tree_hash=candidate_tree_hash,
            host_harness=host_harness,
            host_entrypoint_path=host_entrypoint_path,
            reviewed_source_repository_root=reviewed_source_repository_root,
            protected_state_repository_root=protected_state_repository_root,
            runs_root=runs_root,
            runtime_bundle_root=runtime_bundle_root,
            run_id=run_id,
            user_profile=user_profile,
            host_temp=host_temp,
            host_venv=host_venv,
            protected_manifest=protected,
            platform_qualification=platform_qualification,
            host_before_inventory=sentinel,
            protected_writer_quiescence=quiescence_contract,
            immutable_verifier_identity=immutable_verifier_identity,
            probe_targets=targets,
        )
    )
    immediate_before = capture_protected_host_inventory(
        protected,
        semantic_capture=lambda: _immutable_semantic_capture(
            protected,
            capture=semantic_capture,
        ),
        immutable_verifier_identity=immutable_verifier_identity,
    )
    validate_verifier_root_ownership(immediate_before)
    prelaunch_delta = compare_protected_host_inventories(sentinel, immediate_before)
    quiescence_evidence: ProtectedWriterQuiescenceEvidence | None = None
    if prelaunch_delta.equal:
        quiescence_evidence = build_protected_writer_quiescence_evidence(
            contract=quiescence_contract,
            sentinel=sentinel,
            immediate_before=immediate_before,
        )
    _write_host_audit(contract.paths.audit_root / "host-sentinel.json", sentinel)
    _write_host_audit(
        contract.paths.audit_root / "host-prelaunch-delta.json", prelaunch_delta
    )
    _write_host_audit(
        contract.paths.audit_root / "host-before.json", immediate_before
    )
    if quiescence_evidence is not None:
        _write_host_audit(
            contract.paths.audit_root / "protected-writer-quiescence.json",
            quiescence_evidence,
        )
    _write_host_audit(
        contract.paths.audit_root / "qualification-control.json", contract.control
    )
    _write_host_audit(
        contract.paths.audit_root / "mapping-manifest.json",
        contract.mapping_manifest.contract,
    )

    process: SandboxProcessResult | None = None
    exported: ValidatedBoundaryQualificationExport | None = None
    after: ProtectedHostInventory | None = None
    delta: HostInventoryDelta | None = None
    failure_reason: str | None = (
        None if prelaunch_delta.equal else "protected_host_prelaunch_delta"
    )
    actual_launch = False
    try:
        if quiescence_evidence is not None:
            process = service.launch_boundary_process(
                contract,
                timeout_seconds=qualification_timeout_seconds,
            )
            actual_launch = any(
                item.image_name == "WindowsSandbox.exe"
                for item in process.launched_processes
            )
            if process.timed_out or process.returncode != 0:
                failure_reason = (
                    SandboxFailureReason.SANDBOX_LAUNCH_FAILED.value
                    if process.termination_confirmed
                    else "sandbox_termination_unconfirmed"
                )
            else:
                exported = service.validate_boundary_result(contract)
    except SandboxContractError as exc:
        failure_reason = exc.reason.value
    finally:
        if process is None or process.termination_confirmed:
            try:
                after = capture_protected_host_inventory(
                    protected,
                    semantic_capture=lambda: _immutable_semantic_capture(
                        protected,
                        capture=semantic_capture,
                    ),
                    immutable_verifier_identity=immutable_verifier_identity,
                )
                validate_verifier_root_ownership(after)
                delta = compare_protected_host_inventories(immediate_before, after)
            except (SandboxContractError, OSError, RuntimeError, ValueError):
                failure_reason = "host_after_inventory_failed"
        else:
            failure_reason = "sandbox_termination_unconfirmed"

    if after is not None:
        _write_host_audit(contract.paths.audit_root / "host-after.json", after)
    if delta is not None:
        _write_host_audit(contract.paths.audit_root / "host-delta.json", delta)
    passed = (
        quiescence_evidence is not None
        and exported is not None
        and delta is not None
        and delta.equal
    )
    if not passed and failure_reason is None:
        failure_reason = "protected_host_delta" if delta is not None else "qualification_failed"
    report = _build_iso03_report(
        status=(ISO03QualificationStatus.PASS if passed else ISO03QualificationStatus.FAIL),
        run_id=run_id,
        platform_report=platform_report,
        platform_qualification=platform_qualification,
        static_bindings=contract.static_bindings,
        runner_input_identity=contract.runner_input.runner_input_identity,
        implementation_base_commit_id=(
            contract.runner_input.implementation_base_commit_id
        ),
        candidate_tree_hash=contract.runner_input.candidate_tree_hash,
        host_harness=contract.host_harness,
        protected_writer_quiescence=quiescence_evidence,
        actual_sandbox_launch=actual_launch,
        sandbox_returncode=process.returncode if process is not None else None,
        sandbox_timed_out=process.timed_out if process is not None else False,
        sandbox_termination_confirmed=(
            process.termination_confirmed if process is not None else True
        ),
        sandbox_process_ids=(process.launched_process_ids if process is not None else ()),
        sandbox_process_before=(process.before_processes if process is not None else ()),
        sandbox_process_observed=(
            process.launched_processes if process is not None else ()
        ),
        sandbox_process_after=(process.after_processes if process is not None else ()),
        guest_result=exported.result if exported is not None else None,
        guest_export_identity=exported.export_identity if exported is not None else None,
        host_before_inventory_identity=immediate_before.inventory_identity,
        host_after_inventory_identity=(after.inventory_identity if after is not None else None),
        host_delta=delta,
        semantic_before=_semantic_summary(immediate_before.semantic_inventory),
        semantic_after=(
            _semantic_summary(after.semantic_inventory) if after is not None else None
        ),
        failure_reason=failure_reason,
        ticket06_executed=False,
        ticket07_executed=False,
        provider_model_io=False,
        calendar_refresh_executed=False,
        baseline_frozen=False,
        qualified_v1_enabled=False,
    )
    _write_host_audit(contract.paths.audit_root / "iso03-report.json", report)
    return report

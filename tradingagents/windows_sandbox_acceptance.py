"""Closed ISO-01A Ticket 06 Windows Sandbox acceptance contracts.

This module contains only deterministic, offline contracts shared by the
trusted host and the sealed guest runner.  It does not launch Windows Sandbox
or execute an acceptance command.
"""

from __future__ import annotations

import ast
import json
import re
import stat
from enum import Enum
from hashlib import sha256
from pathlib import Path
from typing import TYPE_CHECKING, Literal, Self

from pydantic import BaseModel, ConfigDict, Field, model_validator

if TYPE_CHECKING:
    from tradingagents.windows_sandbox_runner import (
        ResolvedSandboxMappingManifest,
        WsbConfiguration,
    )

TICKET06_COMMAND_LEDGER_VERSION = "ticket06-sandbox-command-ledger-v1"
_CLOSED_MODEL_CONFIG = ConfigDict(frozen=True, extra="forbid")
_IDENTITY_PATTERN = r"^[a-z0-9][a-z0-9._-]*(?::[a-z0-9][a-z0-9._-]*)*:sha256:[0-9a-f]{64}$"
_CREDENTIAL_ENVIRONMENT_NAME = re.compile(
    r"(?i)(?:API_?KEY|TOKEN|PASSWORD|SECRET|CREDENTIAL|COOKIE)"
)
_TICKET06_FIXED_PROCESS_ENVIRONMENT = (
    ("COMSPEC", r"C:\Windows\System32\cmd.exe"),
    ("GIT_CONFIG_NOSYSTEM", "1"),
    ("GIT_TERMINAL_PROMPT", "0"),
    (
        "PATH",
        (
            r"C:\QCF17\Venv\Scripts;C:\QCF17\Input\Runtime\git\cmd;"
            r"C:\Windows\System32;C:\Windows"
        ),
    ),
    ("PATHEXT", ".COM;.EXE;.BAT;.CMD"),
    ("PYTHONUTF8", "1"),
    ("SYSTEMDRIVE", "C:"),
    ("SYSTEMROOT", r"C:\Windows"),
    ("WINDIR", r"C:\Windows"),
)


def canonical_json_bytes(value: object) -> bytes:
    return json.dumps(
        value,
        allow_nan=False,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")


def content_identity(namespace: str, value: object) -> str:
    return f"{namespace}:sha256:{sha256(canonical_json_bytes(value)).hexdigest()}"


TICKET06_ACCEPTANCE_RESULT_VERSION = "windows-sandbox-ticket06-acceptance-result-v1"
_TICKET06_ACCEPTANCE_RESULT_DESCRIPTOR = {
    "contract_version": TICKET06_ACCEPTANCE_RESULT_VERSION,
    "allowed_export_files": ["completion.json", "result.json"],
    "command_result_limit": 128,
    "qcf_ids": [f"QCF{number}" for number in range(1, 19)],
    "pe_ids": [f"PE{number}" for number in range(1, 16)],
    "ac_ids": [f"AC{number}" for number in range(1, 14)],
    "maximum_file_bytes": 262_144,
    "maximum_export_bytes": 524_288,
    "completion_written_last": True,
    "guest_qcf17_clause_b_authority": False,
    "guest_qcf18_final_authority": False,
    "raw_output_exported": False,
}
TICKET06_ACCEPTANCE_RESULT_SCHEMA_IDENTITY = content_identity(
    "windows-sandbox-ticket06-result-schema:v1",
    _TICKET06_ACCEPTANCE_RESULT_DESCRIPTOR,
)
TICKET06_QUALIFIED_COMMAND_LEDGER_IDENTITY = (
    "ticket06-sandbox-command-ledger:v1:sha256:"
    "5444477692f8a4a517d6e78719c5d21d97550c6a08610bd88596ca76cb68ea23"
)
TICKET06_COMPLETION_MARKER_CONTRACT_IDENTITY = content_identity(
    "windows-sandbox-ticket06-completion-contract:v1",
    {
        "allowed_filename": "completion.json",
        "contract_version": "windows-sandbox-ticket06-completion-v1",
        "write_order": "last",
    },
)


QUALIFIED_RUNNER_SOURCE_FILES = (
    ("scripts/windows_sandbox/bootstrap.ps1", "bootstrap.ps1"),
    (
        "scripts/windows_sandbox/offline_bundle_bootstrap.ps1",
        "install_boundary_runtime.ps1",
    ),
    (
        "scripts/windows_sandbox/run_boundary_qualification.py",
        "run_boundary_qualification.py",
    ),
    (
        "scripts/windows_sandbox/run_ticket06_acceptance.py",
        "run_ticket06_acceptance.py",
    ),
    (
        "scripts/windows_sandbox/run_ticket06_offline_review.py",
        "run_ticket06_offline_review.py",
    ),
    (
        "tradingagents/windows_sandbox_acceptance.py",
        "ticket06_acceptance_contract.py",
    ),
    ("scripts/windows_sandbox/verify_source.py", "verify_source.py"),
)
QUALIFIED_RUNNER_TARGET_FILES = tuple(
    target for _source, target in QUALIFIED_RUNNER_SOURCE_FILES
)


class Ticket06AcceptanceStructuralQualification(BaseModel):
    """Identity-bearing proof that the sealed runner can safely enter ISO-01A mode."""

    model_config = _CLOSED_MODEL_CONFIG

    contract_version: Literal["ticket06-acceptance-runner-structure-v2"] = (
        "ticket06-acceptance-runner-structure-v2"
    )
    runner_input_identity: str = Field(
        pattern=r"^windows-sandbox-qualified-runner-input:v2:sha256:[0-9a-f]{64}$"
    )
    runner_files: tuple[str, ...] = QUALIFIED_RUNNER_TARGET_FILES
    boundary_qualification_mode_present: Literal[True] = True
    ticket06_acceptance_mode_present: Literal[True] = True
    bootstrap_dispatch_target: Literal[
        r"C:\QCF17\Input\Runner\run_ticket06_acceptance.py"
    ] = r"C:\QCF17\Input\Runner\run_ticket06_acceptance.py"
    bootstrap_dispatch_closed: Literal[True] = True
    orchestrator_contract_import_closed: Literal[True] = True
    offline_review_executable_closed: Literal[True] = True
    acceptance_orchestrator_write_order_validated: Literal[True] = True
    host_acceptance_mapping_reuse_validated: Literal[True] = True
    host_acceptance_export_validation_path_validated: Literal[True] = True
    environment_policy_identity: str = Field(pattern=_IDENTITY_PATTERN)
    host_harness_manifest_identity: str = Field(
        pattern=r"^iso03-reviewed-host-harness:v1:sha256:[0-9a-f]{64}$"
    )
    host_acceptance_module_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    boundary_runner_identity: str = Field(
        pattern=r"^iso01-windows-sandbox-runner:v1:sha256:[0-9a-f]{64}$"
    )
    wsb_config_identity: str = Field(
        pattern=r"^windows-sandbox-config:v1:sha256:[0-9a-f]{64}$"
    )
    wsb_config_policy_identity: str = Field(
        pattern=r"^windows-sandbox-config-policy:v1:sha256:[0-9a-f]{64}$"
    )
    mapping_manifest_identity: str = Field(
        pattern=r"^windows-sandbox-mapping-manifest:v1:sha256:[0-9a-f]{64}$"
    )
    mapping_roles: tuple[str, ...]
    command_ledger_identity: str = Field(
        pattern=r"^ticket06-sandbox-command-ledger:v1:sha256:[0-9a-f]{64}$"
    )
    mandatory_command_count: Literal[27] = 27
    full_suite_a_entry_id: Literal["full_suite_a"] = "full_suite_a"
    full_suite_b_entry_id: Literal["full_suite_b"] = "full_suite_b"
    distinct_full_suite_entries: Literal[True] = True
    ticket07_absent: Literal[True] = True
    guest_qcf17_host_clause_b_authority: Literal[False] = False
    guest_qcf18_final_authority: Literal[False] = False
    result_schema_identity: str = TICKET06_ACCEPTANCE_RESULT_SCHEMA_IDENTITY
    result_schema_validated: Literal[True] = True
    completion_marker_contract_identity: str = (
        TICKET06_COMPLETION_MARKER_CONTRACT_IDENTITY
    )
    completion_marker_write_order: Literal["last"] = "last"
    networking_disabled: Literal[True] = True
    input_mappings_read_only: Literal[True] = True
    exactly_one_writable_results_mapping: Literal[True] = True
    additional_host_mapping_required: Literal[False] = False
    network_permission_required: Literal[False] = False
    ticket06_executed: Literal[False] = False
    qualification_identity: str = Field(
        pattern=(
            r"^ticket06-acceptance-runner-structure:v2:sha256:[0-9a-f]{64}$"
        )
    )

    @model_validator(mode="after")
    def _validate_qualification(self) -> Self:
        if self.runner_files != QUALIFIED_RUNNER_TARGET_FILES:
            raise ValueError("qualified runner structural file inventory mismatch")
        if self.mapping_roles != (
            "source",
            "runner",
            "control",
            "runtime_bundle",
            "results_export",
        ):
            raise ValueError("qualified acceptance mapping roles are not closed")
        if self.command_ledger_identity != TICKET06_QUALIFIED_COMMAND_LEDGER_IDENTITY:
            raise ValueError("qualified Ticket 06 command ledger identity mismatch")
        if self.result_schema_identity != TICKET06_ACCEPTANCE_RESULT_SCHEMA_IDENTITY:
            raise ValueError("qualified Ticket 06 result schema identity mismatch")
        if (
            self.completion_marker_contract_identity
            != TICKET06_COMPLETION_MARKER_CONTRACT_IDENTITY
        ):
            raise ValueError("qualified Ticket 06 completion contract mismatch")
        payload = self.model_dump(mode="json", exclude={"qualification_identity"})
        if self.qualification_identity != content_identity(
            "ticket06-acceptance-runner-structure:v2",
            payload,
        ):
            raise ValueError("acceptance-runner structural identity mismatch")
        return self

    @classmethod
    def create(
        cls,
        *,
        runner_input_identity: str,
        environment_policy_identity: str,
        host_harness_manifest_identity: str,
        host_acceptance_module_sha256: str,
        boundary_runner_identity: str,
        wsb_config_identity: str,
        wsb_config_policy_identity: str,
        mapping_manifest_identity: str,
        command_ledger_identity: str,
        result_schema_identity: str = TICKET06_ACCEPTANCE_RESULT_SCHEMA_IDENTITY,
    ) -> Self:
        payload = {
            "contract_version": "ticket06-acceptance-runner-structure-v2",
            "runner_input_identity": runner_input_identity,
            "runner_files": list(QUALIFIED_RUNNER_TARGET_FILES),
            "boundary_qualification_mode_present": True,
            "ticket06_acceptance_mode_present": True,
            "bootstrap_dispatch_target": (
                r"C:\QCF17\Input\Runner\run_ticket06_acceptance.py"
            ),
            "bootstrap_dispatch_closed": True,
            "orchestrator_contract_import_closed": True,
            "offline_review_executable_closed": True,
            "acceptance_orchestrator_write_order_validated": True,
            "host_acceptance_mapping_reuse_validated": True,
            "host_acceptance_export_validation_path_validated": True,
            "environment_policy_identity": environment_policy_identity,
            "host_harness_manifest_identity": host_harness_manifest_identity,
            "host_acceptance_module_sha256": host_acceptance_module_sha256,
            "boundary_runner_identity": boundary_runner_identity,
            "wsb_config_identity": wsb_config_identity,
            "wsb_config_policy_identity": wsb_config_policy_identity,
            "mapping_manifest_identity": mapping_manifest_identity,
            "mapping_roles": [
                "source",
                "runner",
                "control",
                "runtime_bundle",
                "results_export",
            ],
            "command_ledger_identity": command_ledger_identity,
            "mandatory_command_count": 27,
            "full_suite_a_entry_id": "full_suite_a",
            "full_suite_b_entry_id": "full_suite_b",
            "distinct_full_suite_entries": True,
            "ticket07_absent": True,
            "guest_qcf17_host_clause_b_authority": False,
            "guest_qcf18_final_authority": False,
            "result_schema_identity": result_schema_identity,
            "result_schema_validated": True,
            "completion_marker_contract_identity": (
                TICKET06_COMPLETION_MARKER_CONTRACT_IDENTITY
            ),
            "completion_marker_write_order": "last",
            "networking_disabled": True,
            "input_mappings_read_only": True,
            "exactly_one_writable_results_mapping": True,
            "additional_host_mapping_required": False,
            "network_permission_required": False,
            "ticket06_executed": False,
        }
        return cls(
            **payload,
            qualification_identity=content_identity(
                "ticket06-acceptance-runner-structure:v2",
                payload,
            ),
        )


class QualifiedRunnerFile(BaseModel):
    model_config = _CLOSED_MODEL_CONFIG

    relative_path: str = Field(min_length=1, max_length=128)
    byte_length: int = Field(ge=1, le=1_048_576)
    sha256: str = Field(pattern=r"^[0-9a-f]{64}$")


class QualifiedRunnerManifest(BaseModel):
    """One sealed runner input shared by both closed execution modes."""

    model_config = _CLOSED_MODEL_CONFIG

    contract_version: Literal["windows-sandbox-qualified-runner-input-v2"] = (
        "windows-sandbox-qualified-runner-input-v2"
    )
    implementation_base_commit_id: str = Field(pattern=r"^[0-9a-f]{40}$")
    candidate_tree_hash: str = Field(pattern=r"^[0-9a-f]{40}$")
    source_mode: Literal["reviewed_index"] = "reviewed_index"
    files: tuple[QualifiedRunnerFile, ...]
    runner_input_identity: str = Field(
        pattern=(
            r"^windows-sandbox-qualified-runner-input:v2:sha256:[0-9a-f]{64}$"
        )
    )

    @model_validator(mode="after")
    def _validate_manifest(self) -> QualifiedRunnerManifest:
        if tuple(item.relative_path for item in self.files) != QUALIFIED_RUNNER_TARGET_FILES:
            raise ValueError("qualified runner input is incomplete or unordered")
        payload = self.model_dump(mode="json", exclude={"runner_input_identity"})
        expected = content_identity(
            "windows-sandbox-qualified-runner-input:v2",
            payload,
        )
        if self.runner_input_identity != expected:
            raise ValueError("qualified runner input identity mismatch")
        return self


def validate_qualified_runner_input(
    root: Path,
    *,
    expected_identity: str,
) -> QualifiedRunnerManifest:
    """Validate the exact sealed runner files without importing project code."""

    try:
        resolved = root.resolve(strict=True)
        entries = tuple(sorted(resolved.iterdir(), key=lambda item: item.name))
    except OSError as exc:
        raise Ticket06AcceptanceContractError(
            Ticket06AcceptanceFailureReason.INVALID_CONTROL
        ) from exc
    expected_names = tuple(sorted((*QUALIFIED_RUNNER_TARGET_FILES, "runner-manifest.json")))
    if not resolved.is_dir() or tuple(item.name for item in entries) != expected_names:
        raise Ticket06AcceptanceContractError(
            Ticket06AcceptanceFailureReason.INVALID_CONTROL
        )
    try:
        manifest_path = resolved / "runner-manifest.json"
        manifest_metadata = manifest_path.lstat()
        if (
            not stat.S_ISREG(manifest_metadata.st_mode)
            or stat.S_ISLNK(manifest_metadata.st_mode)
            or manifest_metadata.st_size > 65_536
        ):
            raise OSError("unsafe runner manifest")
        manifest = QualifiedRunnerManifest.model_validate_json(
            manifest_path.read_bytes()
        )
        if manifest.runner_input_identity != expected_identity:
            raise ValueError("runner input identity mismatch")
        for row in manifest.files:
            path = resolved / row.relative_path
            metadata = path.lstat()
            if (
                not stat.S_ISREG(metadata.st_mode)
                or stat.S_ISLNK(metadata.st_mode)
                or metadata.st_size != row.byte_length
                or sha256(path.read_bytes()).hexdigest() != row.sha256
            ):
                raise OSError("runner member mismatch")
    except (OSError, TypeError, ValueError) as exc:
        raise Ticket06AcceptanceContractError(
            Ticket06AcceptanceFailureReason.INVALID_CONTROL
        ) from exc
    return manifest


class AcceptanceWorkingDirectoryRole(str, Enum):
    GUEST_WORKSPACE = "guest_workspace"


class AcceptanceResultCategory(str, Enum):
    PERSISTENCE_ISOLATION = "persistence_isolation"
    CAL01 = "cal01"
    CAL02 = "cal02"
    CAL03 = "cal03"
    CAL04 = "cal04"
    TEMPORAL_06B = "temporal_06b"
    QCF = "qcf"
    PE = "pe"
    AC = "ac"
    HISTORY_SECURITY = "history_security"
    SUPPORTING_ACCEPTANCE = "supporting_acceptance"
    LIVE_ISOLATION = "live_isolation"
    NON_INTEGRATION = "non_integration"
    FULL_SUITE_A = "full_suite_a"
    FULL_SUITE_B = "full_suite_b"
    RUFF = "ruff"
    DIFF_WHITESPACE = "diff_whitespace"
    WORKSPACE_STATUS = "workspace_status"
    OFFLINE_SPEC_REVIEW = "offline_spec_review"
    OFFLINE_CODE_QUALITY_REVIEW = "offline_code_quality_review"


class AcceptanceParserKind(str, Enum):
    PYTEST_SUMMARY = "pytest_summary_v1"
    EXIT_STATUS = "exit_status_v1"
    CLEAN_WORKSPACE = "clean_workspace_v1"
    OFFLINE_REVIEW = "offline_review_v1"


class AcceptanceTimeoutPolicy(BaseModel):
    model_config = _CLOSED_MODEL_CONFIG

    policy_version: Literal["ticket06-command-timeout-v1"] = (
        "ticket06-command-timeout-v1"
    )
    timeout_seconds: int = Field(ge=1, le=14_400)
    termination_grace_seconds: int = Field(default=15, ge=1, le=60)
    maximum_captured_output_bytes: int = Field(
        default=262_144,
        ge=4_096,
        le=1_048_576,
    )
    policy_identity: str = Field(pattern=_IDENTITY_PATTERN)

    @model_validator(mode="after")
    def _validate_identity(self) -> AcceptanceTimeoutPolicy:
        payload = self.model_dump(mode="json", exclude={"policy_identity"})
        expected = content_identity("ticket06-command-timeout:v1", payload)
        if self.policy_identity != expected:
            raise ValueError("command timeout policy identity mismatch")
        return self


def _timeout(seconds: int) -> AcceptanceTimeoutPolicy:
    payload = {
        "policy_version": "ticket06-command-timeout-v1",
        "timeout_seconds": seconds,
        "termination_grace_seconds": 15,
        "maximum_captured_output_bytes": 262_144,
    }
    return AcceptanceTimeoutPolicy(
        **payload,
        policy_identity=content_identity("ticket06-command-timeout:v1", payload),
    )


class AcceptanceResultParser(BaseModel):
    model_config = _CLOSED_MODEL_CONFIG

    parser_version: Literal["ticket06-bounded-result-parser-v1"] = (
        "ticket06-bounded-result-parser-v1"
    )
    kind: AcceptanceParserKind
    raw_output_exported: Literal[False] = False
    failure_text_exported: Literal[False] = False
    parser_identity: str = Field(pattern=_IDENTITY_PATTERN)

    @model_validator(mode="after")
    def _validate_identity(self) -> AcceptanceResultParser:
        payload = self.model_dump(mode="json", exclude={"parser_identity"})
        expected = content_identity("ticket06-bounded-result-parser:v1", payload)
        if self.parser_identity != expected:
            raise ValueError("bounded result parser identity mismatch")
        return self


def _parser(kind: AcceptanceParserKind) -> AcceptanceResultParser:
    payload = {
        "parser_version": "ticket06-bounded-result-parser-v1",
        "kind": kind,
        "raw_output_exported": False,
        "failure_text_exported": False,
    }
    return AcceptanceResultParser(
        **payload,
        parser_identity=content_identity("ticket06-bounded-result-parser:v1", payload),
    )


class Ticket06CommandEntry(BaseModel):
    model_config = _CLOSED_MODEL_CONFIG

    command_id: str = Field(pattern=r"^[a-z][a-z0-9_]{2,63}$")
    ordinal: int = Field(ge=1, le=128)
    argv: tuple[str, ...] = Field(min_length=1, max_length=64)
    working_directory_role: AcceptanceWorkingDirectoryRole = (
        AcceptanceWorkingDirectoryRole.GUEST_WORKSPACE
    )
    environment_policy_identity: str = Field(pattern=_IDENTITY_PATTERN)
    result_category: AcceptanceResultCategory
    timeout_policy: AcceptanceTimeoutPolicy
    mandatory: bool
    expected_result_parser: AcceptanceResultParser

    @model_validator(mode="after")
    def _validate_closed_argv(self) -> Ticket06CommandEntry:
        if any(
            not argument
            or "\x00" in argument
            or "\r" in argument
            or "\n" in argument
            for argument in self.argv
        ):
            raise ValueError("command argv contains an unsafe argument")
        if self.argv[0] not in {"$PYTHON", "$GIT"}:
            raise ValueError("command executable role is not closed")
        serialized = "\x00".join(self.argv).casefold()
        if any(
            forbidden in serialized
            for forbidden in (
                "ticket07",
                "ticket_07",
                "qualified_v1",
                "http://",
                "https://",
            )
        ):
            raise ValueError("command argv contains a forbidden operation")
        return self


class Ticket06CommandLedger(BaseModel):
    model_config = _CLOSED_MODEL_CONFIG

    contract_version: Literal["ticket06-sandbox-command-ledger-v1"] = (
        TICKET06_COMMAND_LEDGER_VERSION
    )
    execution_mode: Literal["ticket06_acceptance"] = "ticket06_acceptance"
    entries: tuple[Ticket06CommandEntry, ...]
    stop_after_mandatory_failure: Literal[True] = True
    shell_command_strings_permitted: Literal[False] = False
    dynamic_command_interpolation_permitted: Literal[False] = False
    ledger_identity: str = Field(
        pattern=r"^ticket06-sandbox-command-ledger:v1:sha256:[0-9a-f]{64}$"
    )

    @model_validator(mode="after")
    def _validate_ledger(self) -> Ticket06CommandLedger:
        if tuple(entry.ordinal for entry in self.entries) != tuple(
            range(1, len(self.entries) + 1)
        ):
            raise ValueError("command ledger ordinals are not canonical")
        command_ids = tuple(entry.command_id for entry in self.entries)
        if len(command_ids) != len(set(command_ids)):
            raise ValueError("command ledger IDs are not unique")
        if not all(entry.mandatory for entry in self.entries):
            raise ValueError("Ticket 06 ledger entries must be mandatory")
        if sum(entry.result_category is AcceptanceResultCategory.FULL_SUITE_A for entry in self.entries) != 1:
            raise ValueError("full suite A is missing or duplicated")
        if sum(entry.result_category is AcceptanceResultCategory.FULL_SUITE_B for entry in self.entries) != 1:
            raise ValueError("full suite B is missing or duplicated")
        payload = self.model_dump(mode="json", exclude={"ledger_identity"})
        expected = content_identity("ticket06-sandbox-command-ledger:v1", payload)
        if self.ledger_identity != expected:
            raise ValueError("command ledger identity mismatch")
        return self

    def canonical_json_bytes(self) -> bytes:
        return canonical_json_bytes(self.model_dump(mode="json"))


def _pytest(*paths_and_arguments: str) -> tuple[str, ...]:
    return ("$PYTHON", "-m", "pytest", "-q", *paths_and_arguments)


def build_ticket06_command_ledger(
    environment_policy_identity: str,
) -> Ticket06CommandLedger:
    """Build the one closed Ticket 06 command inventory in execution order."""

    if re.fullmatch(_IDENTITY_PATTERN, environment_policy_identity) is None:
        raise ValueError("environment policy identity is invalid")

    focused_timeout = _timeout(1_800)
    suite_timeout = _timeout(10_800)
    check_timeout = _timeout(600)
    pytest_parser = _parser(AcceptanceParserKind.PYTEST_SUMMARY)
    exit_parser = _parser(AcceptanceParserKind.EXIT_STATUS)
    clean_parser = _parser(AcceptanceParserKind.CLEAN_WORKSPACE)
    review_parser = _parser(AcceptanceParserKind.OFFLINE_REVIEW)
    rows: tuple[
        tuple[
            str,
            tuple[str, ...],
            AcceptanceResultCategory,
            AcceptanceTimeoutPolicy,
            AcceptanceResultParser,
        ],
        ...,
    ] = (
        (
            "systemic_persistence_isolation",
            _pytest("tests/test_pytest_persistence_isolation.py"),
            AcceptanceResultCategory.PERSISTENCE_ISOLATION,
            focused_timeout,
            pytest_parser,
        ),
        (
            "cal01_acquisition_attempts",
            _pytest("tests/test_baostock_session_calendar_acquisition.py"),
            AcceptanceResultCategory.CAL01,
            focused_timeout,
            pytest_parser,
        ),
        (
            "cal02_publication_exact_reader",
            _pytest(
                "tests/test_market_session_calendar_publication.py",
                "tests/test_market_session_calendar_exact_reader.py",
                "tests/test_market_session_calendar_readiness.py",
            ),
            AcceptanceResultCategory.CAL02,
            focused_timeout,
            pytest_parser,
        ),
        (
            "cal03_programmatic_cli_bootstrap",
            _pytest(
                "tests/test_market_session_calendar_bootstrap.py",
                "tests/test_cli.py",
                "tests/test_qualified_routing_rollout.py",
            ),
            AcceptanceResultCategory.CAL03,
            focused_timeout,
            pytest_parser,
        ),
        (
            "cal04_deterministic_acceptance",
            _pytest("tests/test_market_session_calendar_acceptance.py"),
            AcceptanceResultCategory.CAL04,
            focused_timeout,
            pytest_parser,
        ),
        (
            "temporal_06b",
            _pytest(
                "tests/test_financial_metadata_temporal_eligibility.py",
                "tests/test_financial_temporal_proof_projection.py",
                "tests/test_financial_temporal_checkpoint_compatibility.py",
            ),
            AcceptanceResultCategory.TEMPORAL_06B,
            focused_timeout,
            pytest_parser,
        ),
        (
            "qcf_matrix",
            _pytest("tests/test_qualified_routing_composition_acceptance.py"),
            AcceptanceResultCategory.QCF,
            focused_timeout,
            pytest_parser,
        ),
        (
            "composition_contracts",
            _pytest(
                "tests/test_qualified_routing_composition_contract.py",
                "tests/test_production_qualified_provider_composition.py",
                "tests/test_qualified_routing_static_registration.py",
                "tests/test_qualified_routing_managed_lifecycle.py",
                "tests/test_qualified_routing_factory_checkpoint_restore.py",
            ),
            AcceptanceResultCategory.SUPPORTING_ACCEPTANCE,
            focused_timeout,
            pytest_parser,
        ),
        (
            "eligibility_context",
            _pytest(
                "tests/test_financial_eligibility_acquisition_context.py",
                "tests/test_financial_eligibility_metadata_acquisition.py",
                "tests/test_financial_request_factory_context_binding.py",
            ),
            AcceptanceResultCategory.SUPPORTING_ACCEPTANCE,
            focused_timeout,
            pytest_parser,
        ),
        (
            "production_source_bindings",
            _pytest(
                "tests/test_qualified_production_source_bindings.py",
                "tests/test_qualified_routing_eligibility_context_lifecycle.py",
                "tests/test_qualified_routing_checkpoint_context_restore.py",
            ),
            AcceptanceResultCategory.SUPPORTING_ACCEPTANCE,
            focused_timeout,
            pytest_parser,
        ),
        (
            "indicator_metadata",
            _pytest(
                "tests/test_financial_indicator_field_semantics.py",
                "tests/test_financial_indicator_metadata_compatibility.py",
                "tests/test_financial_indicator_compatibility_persistence.py",
            ),
            AcceptanceResultCategory.SUPPORTING_ACCEPTANCE,
            focused_timeout,
            pytest_parser,
        ),
        (
            "provider_adapters",
            _pytest(
                "tests/test_tushare_statement_adapters.py",
                "tests/test_tushare_indicator_adapter.py",
                "tests/test_tushare_factor_name_adapters.py",
                "tests/test_akshare_sina_statement_adapters.py",
                "tests/test_baostock_capability_adapters.py",
                "tests/test_baostock_ratio_adapters.py",
            ),
            AcceptanceResultCategory.SUPPORTING_ACCEPTANCE,
            focused_timeout,
            pytest_parser,
        ),
        (
            "provider_coordination",
            _pytest(
                "tests/test_provider_subrequest_cache.py",
                "tests/test_provider_request_coordinator.py",
                "tests/test_mainland_physical_attempt_coordination.py",
                "tests/test_yahoo_retry_coordination.py",
            ),
            AcceptanceResultCategory.SUPPORTING_ACCEPTANCE,
            focused_timeout,
            pytest_parser,
        ),
        (
            "graph_shadow_pe",
            _pytest(
                "tests/test_financial_tool_dispatcher.py",
                "tests/test_financial_graph_dispatch.py",
                "tests/test_financial_capability_routing_selection.py",
                "tests/test_qualified_routing_rollout.py",
                "tests/test_corrected_shadow_joint_acceptance.py",
            ),
            AcceptanceResultCategory.PE,
            focused_timeout,
            pytest_parser,
        ),
        (
            "checkpoint_reporting_evidence",
            _pytest(
                "tests/test_checkpoint_resume.py",
                "tests/test_financial_version_persistence.py",
                "tests/test_financial_manifest_evidence_reporting.py",
                "tests/test_reporting.py",
                "tests/test_decision_audit.py",
                "tests/test_admitted_evidence_binding.py",
                "tests/test_recorded_evidence_replay.py",
                "tests/test_evidence_gates.py",
            ),
            AcceptanceResultCategory.SUPPORTING_ACCEPTANCE,
            focused_timeout,
            pytest_parser,
        ),
        (
            "history_security_checks",
            _pytest(
                "tests/test_ticket06_immutable_state_verifier.py",
                "tests/test_recorded_evidence_replay.py",
            ),
            AcceptanceResultCategory.HISTORY_SECURITY,
            focused_timeout,
            pytest_parser,
        ),
        (
            "market_history_ac",
            _pytest(
                "tests/test_market_snapshot.py",
                "tests/test_market_history_store.py",
                "tests/test_market_history_reconstruction.py",
                "tests/test_market_history_shadow.py",
                "tests/test_current_tradeability.py",
            ),
            AcceptanceResultCategory.AC,
            focused_timeout,
            pytest_parser,
        ),
        (
            "financial_period_pe",
            _pytest(
                "tests/test_financial_period_targets.py",
                "tests/test_financial_period_availability.py",
                "tests/test_financial_period_contracts.py",
            ),
            AcceptanceResultCategory.PE,
            focused_timeout,
            pytest_parser,
        ),
        (
            "live_test_isolation",
            _pytest("tests/test_live_test_isolation.py"),
            AcceptanceResultCategory.LIVE_ISOLATION,
            focused_timeout,
            pytest_parser,
        ),
        (
            "non_integration",
            _pytest("-m", "not integration"),
            AcceptanceResultCategory.NON_INTEGRATION,
            suite_timeout,
            pytest_parser,
        ),
        (
            "full_suite_a",
            _pytest(),
            AcceptanceResultCategory.FULL_SUITE_A,
            suite_timeout,
            pytest_parser,
        ),
        (
            "full_suite_b",
            _pytest(),
            AcceptanceResultCategory.FULL_SUITE_B,
            suite_timeout,
            pytest_parser,
        ),
        (
            "ruff",
            ("$PYTHON", "-m", "ruff", "check", "tradingagents", "cli", "tests"),
            AcceptanceResultCategory.RUFF,
            check_timeout,
            exit_parser,
        ),
        (
            "diff_whitespace",
            ("$GIT", "-c", "core.hooksPath=NUL", "diff", "--check"),
            AcceptanceResultCategory.DIFF_WHITESPACE,
            check_timeout,
            exit_parser,
        ),
        (
            "workspace_status",
            ("$GIT", "-c", "core.hooksPath=NUL", "status", "--short"),
            AcceptanceResultCategory.WORKSPACE_STATUS,
            check_timeout,
            clean_parser,
        ),
        (
            "offline_spec_compliance_review",
            (
                "$PYTHON",
                r"C:\QCF17\Input\Runner\run_ticket06_offline_review.py",
                "--stage",
                "spec_compliance",
            ),
            AcceptanceResultCategory.OFFLINE_SPEC_REVIEW,
            check_timeout,
            review_parser,
        ),
        (
            "offline_code_quality_review",
            (
                "$PYTHON",
                r"C:\QCF17\Input\Runner\run_ticket06_offline_review.py",
                "--stage",
                "code_quality",
            ),
            AcceptanceResultCategory.OFFLINE_CODE_QUALITY_REVIEW,
            check_timeout,
            review_parser,
        ),
    )
    entries = tuple(
        Ticket06CommandEntry(
            command_id=command_id,
            ordinal=ordinal,
            argv=argv,
            environment_policy_identity=environment_policy_identity,
            result_category=category,
            timeout_policy=timeout_policy,
            mandatory=True,
            expected_result_parser=parser,
        )
        for ordinal, (command_id, argv, category, timeout_policy, parser) in enumerate(
            rows,
            start=1,
        )
    )
    payload = {
        "contract_version": TICKET06_COMMAND_LEDGER_VERSION,
        "execution_mode": "ticket06_acceptance",
        "entries": [entry.model_dump(mode="json") for entry in entries],
        "stop_after_mandatory_failure": True,
        "shell_command_strings_permitted": False,
        "dynamic_command_interpolation_permitted": False,
    }
    return Ticket06CommandLedger(
        **payload,
        ledger_identity=content_identity("ticket06-sandbox-command-ledger:v1", payload),
    )


class Ticket06AcceptanceFailureReason(str, Enum):
    INVALID_CONTROL = "invalid_ticket06_acceptance_control"
    INVALID_LEDGER = "invalid_ticket06_command_ledger"
    INCOMPLETE_RESULT = "incomplete_ticket06_acceptance_result"
    RESULT_IDENTITY_MISMATCH = "ticket06_result_identity_mismatch"
    UNSAFE_EXPORT = "unsafe_ticket06_acceptance_export"
    INVALID_COMPLETION_MARKER = "invalid_ticket06_completion_marker"


class Ticket06AcceptanceContractError(ValueError):
    """Typed, bounded refusal for an ISO-01A acceptance contract violation."""

    def __init__(self, reason: Ticket06AcceptanceFailureReason) -> None:
        self.reason = reason
        super().__init__(reason.value)


class AcceptanceCommandStatus(str, Enum):
    NOT_RUN = "not_run"
    PASS = "pass"
    FAIL = "fail"
    TIMEOUT = "timeout"


class AcceptanceEvidenceStatus(str, Enum):
    NOT_RUN = "not_run"
    PASS = "pass"
    FAIL = "fail"


class Ticket06CommandResult(BaseModel):
    model_config = _CLOSED_MODEL_CONFIG

    command_id: str = Field(pattern=r"^[a-z][a-z0-9_]{2,63}$")
    ordinal: int = Field(ge=1, le=128)
    result_category: AcceptanceResultCategory
    status: AcceptanceCommandStatus
    attempted: bool
    mandatory: bool
    returncode: int | None = Field(default=None, ge=-(2**31), le=(2**31) - 1)
    timed_out: bool = False
    output_limit_exceeded: bool = False
    passed: int = Field(default=0, ge=0, le=100_000)
    failed: int = Field(default=0, ge=0, le=100_000)
    skipped: int = Field(default=0, ge=0, le=100_000)
    xfailed: int = Field(default=0, ge=0, le=100_000)
    xpassed: int = Field(default=0, ge=0, le=100_000)
    deselected: int = Field(default=0, ge=0, le=100_000)
    parser_identity: str = Field(pattern=_IDENTITY_PATTERN)
    summary_identity: str = Field(pattern=_IDENTITY_PATTERN)

    @model_validator(mode="after")
    def _validate_result(self) -> Ticket06CommandResult:
        counts = (
            self.passed,
            self.failed,
            self.skipped,
            self.xfailed,
            self.xpassed,
            self.deselected,
        )
        if self.status is AcceptanceCommandStatus.NOT_RUN:
            if self.attempted or self.returncode is not None or any(counts):
                raise ValueError("not-run command cannot carry execution data")
            if self.timed_out or self.output_limit_exceeded:
                raise ValueError("not-run command cannot carry failure data")
        elif not self.attempted:
            raise ValueError("attempted command status is inconsistent")
        if self.status is AcceptanceCommandStatus.PASS and (
            self.returncode != 0
            or self.timed_out
            or self.output_limit_exceeded
            or self.failed
        ):
            raise ValueError("failed command cannot be represented as pass")
        if self.status is AcceptanceCommandStatus.TIMEOUT and not self.timed_out:
            raise ValueError("timeout command result is inconsistent")
        if self.timed_out and self.status is not AcceptanceCommandStatus.TIMEOUT:
            raise ValueError("timed-out command cannot use another status")
        payload = self.model_dump(mode="json", exclude={"summary_identity"})
        expected = content_identity("ticket06-command-result:v1", payload)
        if self.summary_identity != expected:
            raise ValueError("command result summary identity mismatch")
        return self


def build_ticket06_command_result(
    entry: Ticket06CommandEntry,
    *,
    status: AcceptanceCommandStatus,
    returncode: int | None = None,
    passed: int = 0,
    failed: int = 0,
    skipped: int = 0,
    xfailed: int = 0,
    xpassed: int = 0,
    deselected: int = 0,
    output_limit_exceeded: bool = False,
) -> Ticket06CommandResult:
    payload = {
        "command_id": entry.command_id,
        "ordinal": entry.ordinal,
        "result_category": entry.result_category,
        "status": status,
        "attempted": status is not AcceptanceCommandStatus.NOT_RUN,
        "mandatory": entry.mandatory,
        "returncode": returncode,
        "timed_out": status is AcceptanceCommandStatus.TIMEOUT,
        "output_limit_exceeded": output_limit_exceeded,
        "passed": passed,
        "failed": failed,
        "skipped": skipped,
        "xfailed": xfailed,
        "xpassed": xpassed,
        "deselected": deselected,
        "parser_identity": entry.expected_result_parser.parser_identity,
    }
    return Ticket06CommandResult(
        **payload,
        summary_identity=content_identity("ticket06-command-result:v1", payload),
    )


class Ticket06AcceptanceStaticBindings(BaseModel):
    model_config = _CLOSED_MODEL_CONFIG

    execution_mode: Literal["ticket06_acceptance"] = "ticket06_acceptance"
    source_commit_id: str = Field(pattern=r"^[0-9a-f]{40}$")
    source_tree_hash: str = Field(pattern=r"^[0-9a-f]{40}$")
    source_input_identity: str = Field(pattern=_IDENTITY_PATTERN)
    source_manifest_identity: str = Field(pattern=_IDENTITY_PATTERN)
    source_archive_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    reconstructed_source_tree_identity: str = Field(pattern=_IDENTITY_PATTERN)
    runtime_bundle_identity: str = Field(pattern=_IDENTITY_PATTERN)
    runtime_bundle_manifest_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    runtime_bundle_contract_identity: str = Field(pattern=_IDENTITY_PATTERN)
    runner_identity: str = Field(pattern=_IDENTITY_PATTERN)
    runner_input_identity: str = Field(pattern=_IDENTITY_PATTERN)
    wsb_config_identity: str = Field(pattern=_IDENTITY_PATTERN)
    protected_root_manifest_identity: str = Field(pattern=_IDENTITY_PATTERN)
    mapping_manifest_identity: str = Field(pattern=_IDENTITY_PATTERN)
    guest_workspace_identity: str = Field(pattern=_IDENTITY_PATTERN)
    environment_policy_identity: str = Field(pattern=_IDENTITY_PATTERN)
    control_identity: str = Field(pattern=_IDENTITY_PATTERN)
    frozen_baseline_identity: str = Field(pattern=_IDENTITY_PATTERN)
    boundary_qualification_identity: str = Field(pattern=_IDENTITY_PATTERN)
    command_ledger_identity: str = Field(
        pattern=r"^ticket06-sandbox-command-ledger:v1:sha256:[0-9a-f]{64}$"
    )


class Ticket06ControlSource(BaseModel):
    model_config = _CLOSED_MODEL_CONFIG

    contract_version: Literal["windows-sandbox-reviewed-source-v1"] = (
        "windows-sandbox-reviewed-source-v1"
    )
    commit_id: str = Field(pattern=r"^[0-9a-f]{40}$")
    tree_hash: str = Field(pattern=r"^[0-9a-f]{40}$")
    git_object_format: Literal["sha1"] = "sha1"
    commit_object_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    archive_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    source_manifest_identity: str = Field(pattern=_IDENTITY_PATTERN)
    source_input_identity: str = Field(pattern=_IDENTITY_PATTERN)
    tracked_file_count: int = Field(ge=1, le=65_536)
    run_live_environment: tuple[str, ...]

    @model_validator(mode="after")
    def _validate_source(self) -> Ticket06ControlSource:
        if tuple(sorted(set(self.run_live_environment))) != self.run_live_environment:
            raise ValueError("source RUN_LIVE names are not canonical")
        payload = self.model_dump(mode="json", exclude={"source_input_identity"})
        if self.source_input_identity != content_identity(
            "windows-sandbox-reviewed-source:v1",
            payload,
        ):
            raise ValueError("source input identity mismatch")
        return self


class Ticket06OfflineSourceBinding(BaseModel):
    model_config = _CLOSED_MODEL_CONFIG

    contract_version: Literal["windows-sandbox-reviewed-source-v1"]
    commit_id: str = Field(pattern=r"^[0-9a-f]{40}$")
    tree_hash: str = Field(pattern=r"^[0-9a-f]{40}$")
    source_input_identity: str = Field(pattern=_IDENTITY_PATTERN)
    source_manifest_identity: str = Field(pattern=_IDENTITY_PATTERN)
    source_archive_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")


class Ticket06ControlRuntime(BaseModel):
    model_config = _CLOSED_MODEL_CONFIG

    contract_version: Literal["windows-sandbox-runtime-bundle-input-v1"] = (
        "windows-sandbox-runtime-bundle-input-v1"
    )
    state: Literal["sealed"] = "sealed"
    mapped_read_only: Literal[True] = True
    offline_required: Literal[True] = True
    network_install_permitted: Literal[False] = False
    hash_algorithm: Literal["sha256"] = "sha256"
    python_install_target: Literal[r"C:\QCF17\Python"] = r"C:\QCF17\Python"
    virtual_environment_target: Literal[r"C:\QCF17\Venv"] = r"C:\QCF17\Venv"
    bundle_manifest_identity: str = Field(pattern=_IDENTITY_PATTERN)
    bundle_manifest_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    bootstrap_relative_path: str = Field(min_length=1, max_length=260)
    git_relative_path: Literal["git/cmd/git.exe"] = "git/cmd/git.exe"
    runtime_bundle_contract_identity: str = Field(pattern=_IDENTITY_PATTERN)

    @model_validator(mode="after")
    def _validate_runtime(self) -> Ticket06ControlRuntime:
        payload = self.model_dump(
            mode="json",
            exclude={"runtime_bundle_contract_identity"},
        )
        if self.runtime_bundle_contract_identity != content_identity(
            "windows-sandbox-runtime-bundle-input:v1",
            payload,
        ):
            raise ValueError("runtime bundle control identity mismatch")
        return self


class Ticket06ControlGuestLayout(BaseModel):
    model_config = _CLOSED_MODEL_CONFIG

    contract_version: Literal["windows-sandbox-guest-layout-v1"] = (
        "windows-sandbox-guest-layout-v1"
    )
    guest_root: Literal[r"C:\QCF17"] = r"C:\QCF17"
    workspace_repository: Literal[r"C:\QCF17\Work\Repository"] = (
        r"C:\QCF17\Work\Repository"
    )
    python_install: Literal[r"C:\QCF17\Python"] = r"C:\QCF17\Python"
    virtual_environment: Literal[r"C:\QCF17\Venv"] = r"C:\QCF17\Venv"
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
    guest_output: Literal[r"C:\QCF17\Output"] = r"C:\QCF17\Output"
    layout_identity: str = Field(pattern=_IDENTITY_PATTERN)

    @model_validator(mode="after")
    def _validate_layout(self) -> Ticket06ControlGuestLayout:
        payload = self.model_dump(mode="json", exclude={"layout_identity"})
        if self.layout_identity != content_identity(
            "windows-sandbox-guest-layout:v1",
            payload,
        ):
            raise ValueError("guest layout identity mismatch")
        return self


class Ticket06ControlEnvironment(BaseModel):
    model_config = _CLOSED_MODEL_CONFIG

    contract_version: Literal["windows-sandbox-environment-policy-v1"] = (
        "windows-sandbox-environment-policy-v1"
    )
    variables: tuple[tuple[str, str], ...]
    inherited_host_environment_permitted: Literal[False] = False
    credential_environment_permitted: Literal[False] = False
    policy_identity: str = Field(pattern=_IDENTITY_PATTERN)

    @model_validator(mode="after")
    def _validate_environment(self) -> Ticket06ControlEnvironment:
        names = tuple(name for name, _value in self.variables)
        values = dict(self.variables)
        if names != tuple(sorted(set(names))) or len(values) != len(names):
            raise ValueError("environment variables are not canonical")
        if values.get("PYTHON_DOTENV_DISABLED") != "1" or any(
            value != "0"
            for name, value in self.variables
            if name.startswith("RUN_LIVE_")
        ):
            raise ValueError("environment policy is unsafe")
        if any(_CREDENTIAL_ENVIRONMENT_NAME.search(name) for name in values):
            raise ValueError("credential environment variable is forbidden")
        if any(values.get(name) != value for name, value in _TICKET06_FIXED_PROCESS_ENVIRONMENT):
            raise ValueError("fixed guest process environment is incomplete")
        payload = self.model_dump(mode="json", exclude={"policy_identity"})
        if self.policy_identity != content_identity(
            "windows-sandbox-environment-policy:v1",
            payload,
        ):
            raise ValueError("environment policy identity mismatch")
        return self


def build_ticket06_control_environment(
    base_variables: tuple[tuple[str, str], ...],
) -> Ticket06ControlEnvironment:
    """Bind the qualified guest-local policy plus fixed Windows process inputs."""

    names = tuple(name for name, _value in base_variables)
    variables = dict(base_variables)
    if names != tuple(sorted(set(names))) or len(variables) != len(names):
        raise ValueError("base environment variables are not canonical")
    for name, value in _TICKET06_FIXED_PROCESS_ENVIRONMENT:
        existing = variables.get(name)
        if existing is not None and existing != value:
            raise ValueError("base environment contradicts fixed guest policy")
        variables[name] = value
    ordered = tuple(sorted(variables.items()))
    payload = {
        "contract_version": "windows-sandbox-environment-policy-v1",
        "variables": [list(item) for item in ordered],
        "inherited_host_environment_permitted": False,
        "credential_environment_permitted": False,
    }
    return Ticket06ControlEnvironment(
        variables=ordered,
        policy_identity=content_identity(
            "windows-sandbox-environment-policy:v1",
            payload,
        ),
    )


class Ticket06AcceptanceControlBase(BaseModel):
    model_config = _CLOSED_MODEL_CONFIG

    contract_version: Literal["windows-sandbox-ticket06-control-base-v1"] = (
        "windows-sandbox-ticket06-control-base-v1"
    )
    execution_mode: Literal["ticket06_acceptance"] = "ticket06_acceptance"
    run_id: str = Field(pattern=r"^[a-z0-9][a-z0-9._-]{7,63}$")
    source: Ticket06ControlSource
    runtime_bundle: Ticket06ControlRuntime
    runtime_bundle_identity: str = Field(pattern=_IDENTITY_PATTERN)
    runner_input_identity: str = Field(
        pattern=r"^windows-sandbox-qualified-runner-input:v2:sha256:[0-9a-f]{64}$"
    )
    protected_root_manifest_identity: str = Field(pattern=_IDENTITY_PATTERN)
    boundary_qualification_identity: str = Field(pattern=_IDENTITY_PATTERN)
    frozen_baseline_identity: str = Field(pattern=_IDENTITY_PATTERN)
    guest_layout: Ticket06ControlGuestLayout
    environment: Ticket06ControlEnvironment
    command_ledger: Ticket06CommandLedger
    result_schema_identity: str = TICKET06_ACCEPTANCE_RESULT_SCHEMA_IDENTITY
    networking_disabled: Literal[True] = True
    input_mappings_read_only: Literal[True] = True
    exactly_one_writable_results_mapping: Literal[True] = True
    credential_inputs_present: Literal[False] = False
    ticket07_permitted: Literal[False] = False
    provider_model_io_permitted: Literal[False] = False
    calendar_refresh_permitted: Literal[False] = False
    qualified_v1_activation_permitted: Literal[False] = False

    @model_validator(mode="after")
    def _validate_base(self) -> Ticket06AcceptanceControlBase:
        if self.result_schema_identity != TICKET06_ACCEPTANCE_RESULT_SCHEMA_IDENTITY:
            raise ValueError("Ticket 06 result schema identity mismatch")
        if self.command_ledger != build_ticket06_command_ledger(
            self.environment.policy_identity
        ):
            raise ValueError("Ticket 06 control ledger mismatch")
        if self.runtime_bundle_identity != self.runtime_bundle.bundle_manifest_identity:
            raise ValueError("Ticket 06 runtime identity mismatch")
        return self


class Ticket06AcceptanceControlEnvelope(BaseModel):
    model_config = _CLOSED_MODEL_CONFIG

    contract_version: Literal["windows-sandbox-ticket06-control-envelope-v1"] = (
        "windows-sandbox-ticket06-control-envelope-v1"
    )
    base: Ticket06AcceptanceControlBase
    control_identity: str = Field(
        pattern=r"^windows-sandbox-ticket06-control:v1:sha256:[0-9a-f]{64}$"
    )
    source: Ticket06OfflineSourceBinding
    runtime_bundle: Ticket06ControlRuntime
    derived: Ticket06AcceptanceStaticBindings

    @model_validator(mode="after")
    def _validate_envelope(self) -> Ticket06AcceptanceControlEnvelope:
        expected_control_identity = content_identity(
            "windows-sandbox-ticket06-control:v1",
            self.base.model_dump(mode="json"),
        )
        if self.control_identity != expected_control_identity:
            raise ValueError("Ticket 06 control identity mismatch")
        source = self.base.source
        if self.source != Ticket06OfflineSourceBinding(
            contract_version=source.contract_version,
            commit_id=source.commit_id,
            tree_hash=source.tree_hash,
            source_input_identity=source.source_input_identity,
            source_manifest_identity=source.source_manifest_identity,
            source_archive_sha256=source.archive_sha256,
        ) or self.runtime_bundle != self.base.runtime_bundle:
            raise ValueError("Ticket 06 offline bootstrap alias mismatch")
        expected_bindings = {
            "source_commit_id": source.commit_id,
            "source_tree_hash": source.tree_hash,
            "source_input_identity": source.source_input_identity,
            "source_manifest_identity": source.source_manifest_identity,
            "source_archive_sha256": source.archive_sha256,
            "reconstructed_source_tree_identity": content_identity(
                "windows-sandbox-reconstructed-source:v1",
                {
                    "contract_version": (
                        "windows-sandbox-guest-source-verification-v1"
                    ),
                    "commit_id": source.commit_id,
                    "project_code_imported": False,
                    "source_manifest_identity": source.source_manifest_identity,
                    "synthetic_git_worktree": True,
                    "tracked_file_count": source.tracked_file_count,
                    "tree_hash": source.tree_hash,
                },
            ),
            "runtime_bundle_identity": self.base.runtime_bundle_identity,
            "runtime_bundle_manifest_sha256": (
                self.base.runtime_bundle.bundle_manifest_sha256
            ),
            "runtime_bundle_contract_identity": (
                self.base.runtime_bundle.runtime_bundle_contract_identity
            ),
            "runner_input_identity": self.base.runner_input_identity,
            "protected_root_manifest_identity": (
                self.base.protected_root_manifest_identity
            ),
            "environment_policy_identity": self.base.environment.policy_identity,
            "control_identity": self.control_identity,
            "frozen_baseline_identity": self.base.frozen_baseline_identity,
            "boundary_qualification_identity": (
                self.base.boundary_qualification_identity
            ),
            "command_ledger_identity": self.base.command_ledger.ledger_identity,
        }
        derived = self.derived.model_dump(mode="json")
        if any(derived[name] != value for name, value in expected_bindings.items()):
            raise ValueError("Ticket 06 derived static binding mismatch")
        return self


def build_ticket06_acceptance_control_envelope(
    *,
    base: Ticket06AcceptanceControlBase,
    runner_identity: str,
    wsb_config_identity: str,
    mapping_manifest_identity: str,
    guest_workspace_identity: str,
) -> Ticket06AcceptanceControlEnvelope:
    """Finalize the acceptance envelope after mapping/runner identities exist."""

    control_identity = content_identity(
        "windows-sandbox-ticket06-control:v1",
        base.model_dump(mode="json"),
    )
    source = base.source
    static = Ticket06AcceptanceStaticBindings(
        source_commit_id=source.commit_id,
        source_tree_hash=source.tree_hash,
        source_input_identity=source.source_input_identity,
        source_manifest_identity=source.source_manifest_identity,
        source_archive_sha256=source.archive_sha256,
        reconstructed_source_tree_identity=content_identity(
            "windows-sandbox-reconstructed-source:v1",
            {
                "contract_version": "windows-sandbox-guest-source-verification-v1",
                "commit_id": source.commit_id,
                "project_code_imported": False,
                "source_manifest_identity": source.source_manifest_identity,
                "synthetic_git_worktree": True,
                "tracked_file_count": source.tracked_file_count,
                "tree_hash": source.tree_hash,
            },
        ),
        runtime_bundle_identity=base.runtime_bundle_identity,
        runtime_bundle_manifest_sha256=base.runtime_bundle.bundle_manifest_sha256,
        runtime_bundle_contract_identity=(
            base.runtime_bundle.runtime_bundle_contract_identity
        ),
        runner_identity=runner_identity,
        runner_input_identity=base.runner_input_identity,
        wsb_config_identity=wsb_config_identity,
        protected_root_manifest_identity=base.protected_root_manifest_identity,
        mapping_manifest_identity=mapping_manifest_identity,
        guest_workspace_identity=guest_workspace_identity,
        environment_policy_identity=base.environment.policy_identity,
        control_identity=control_identity,
        frozen_baseline_identity=base.frozen_baseline_identity,
        boundary_qualification_identity=base.boundary_qualification_identity,
        command_ledger_identity=base.command_ledger.ledger_identity,
    )
    return Ticket06AcceptanceControlEnvelope(
        base=base,
        control_identity=control_identity,
        source=Ticket06OfflineSourceBinding(
            contract_version=source.contract_version,
            commit_id=source.commit_id,
            tree_hash=source.tree_hash,
            source_input_identity=source.source_input_identity,
            source_manifest_identity=source.source_manifest_identity,
            source_archive_sha256=source.archive_sha256,
        ),
        runtime_bundle=base.runtime_bundle,
        derived=static,
    )


class AcceptanceCriterionResult(BaseModel):
    model_config = _CLOSED_MODEL_CONFIG

    criterion_id: str = Field(pattern=r"^(?:QCF(?:[1-9]|1[0-8])|PE(?:[1-9]|1[0-5])|AC(?:[1-9]|1[0-3]))$")
    evidence_status: AcceptanceEvidenceStatus
    command_ids: tuple[str, ...] = Field(min_length=1, max_length=32)
    evidence_identity: str = Field(pattern=_IDENTITY_PATTERN)

    @model_validator(mode="after")
    def _validate_identity(self) -> AcceptanceCriterionResult:
        payload = self.model_dump(mode="json", exclude={"evidence_identity"})
        expected = content_identity("ticket06-criterion-evidence:v1", payload)
        if self.evidence_identity != expected:
            raise ValueError("criterion evidence identity mismatch")
        return self


class AcceptanceCategorySummary(BaseModel):
    model_config = _CLOSED_MODEL_CONFIG

    category: AcceptanceResultCategory
    status: AcceptanceEvidenceStatus
    command_ids: tuple[str, ...] = Field(min_length=1, max_length=32)
    summary_identity: str = Field(pattern=_IDENTITY_PATTERN)

    @model_validator(mode="after")
    def _validate_identity(self) -> AcceptanceCategorySummary:
        payload = self.model_dump(mode="json", exclude={"summary_identity"})
        expected = content_identity("ticket06-category-summary:v1", payload)
        if self.summary_identity != expected:
            raise ValueError("acceptance category summary identity mismatch")
        return self


class Ticket06AuthorityBoundary(BaseModel):
    model_config = _CLOSED_MODEL_CONFIG

    qcf17_guest_clause_a: AcceptanceEvidenceStatus
    qcf17_host_clause_b: Literal["not_reported_host_owned"] = (
        "not_reported_host_owned"
    )
    qcf17_final: Literal["not_reported_host_owned"] = "not_reported_host_owned"
    qcf18_guest_evidence: AcceptanceEvidenceStatus
    qcf18_final: Literal["not_reported_conditional_on_host_qcf17"] = (
        "not_reported_conditional_on_host_qcf17"
    )
    host_qcf17_authority_claimed: Literal[False] = False
    host_qcf18_authority_claimed: Literal[False] = False


class NoNetworkNoLiveEvidence(BaseModel):
    model_config = _CLOSED_MODEL_CONFIG

    networking_disabled: Literal[True] = True
    ledger_network_commands_absent: Literal[True] = True
    live_environment: tuple[tuple[str, str], ...] = Field(min_length=4, max_length=128)
    dotenv_disabled: Literal[True] = True
    credentials_present: Literal[False] = False
    provider_commands_executed: Literal[False] = False
    model_commands_executed: Literal[False] = False
    evidence_identity: str = Field(pattern=_IDENTITY_PATTERN)

    @model_validator(mode="after")
    def _validate_evidence(self) -> NoNetworkNoLiveEvidence:
        names = tuple(name for name, _value in self.live_environment)
        values = dict(self.live_environment)
        if names != tuple(sorted(set(names))) or len(values) != len(names):
            raise ValueError("live environment evidence is not canonical")
        if values.get("PYTHON_DOTENV_DISABLED") != "1" or any(
            value != "0"
            for name, value in self.live_environment
            if name.startswith("RUN_LIVE_")
        ):
            raise ValueError("live environment evidence is unsafe")
        required = {
            "RUN_LIVE_DEEPSEEK_TESTS",
            "RUN_LIVE_PROVIDER_TESTS",
            "RUN_LIVE_TUSHARE_TESTS",
        }
        if not required.issubset(values):
            raise ValueError("required live opt-ins are absent")
        payload = self.model_dump(mode="json", exclude={"evidence_identity"})
        expected = content_identity("ticket06-no-network-no-live:v1", payload)
        if self.evidence_identity != expected:
            raise ValueError("no-network/no-live evidence identity mismatch")
        return self


class Ticket06AcceptanceResult(BaseModel):
    model_config = _CLOSED_MODEL_CONFIG

    contract_version: Literal["windows-sandbox-ticket06-acceptance-result-v1"] = (
        TICKET06_ACCEPTANCE_RESULT_VERSION
    )
    result_schema_identity: str = TICKET06_ACCEPTANCE_RESULT_SCHEMA_IDENTITY
    static: Ticket06AcceptanceStaticBindings
    guest_claim: Literal["pass", "fail"]
    command_results: tuple[Ticket06CommandResult, ...] = Field(
        min_length=1,
        max_length=128,
    )
    category_summaries: tuple[AcceptanceCategorySummary, ...]
    qcf: tuple[AcceptanceCriterionResult, ...]
    pe: tuple[AcceptanceCriterionResult, ...]
    ac: tuple[AcceptanceCriterionResult, ...]
    authority: Ticket06AuthorityBoundary
    no_network_no_live: NoNetworkNoLiveEvidence
    completion_marker_written_by_orchestrator: Literal[True] = True
    result_identity: str = Field(
        pattern=r"^windows-sandbox-ticket06-result:v1:sha256:[0-9a-f]{64}$"
    )

    @model_validator(mode="after")
    def _validate_result(self) -> Ticket06AcceptanceResult:
        if self.result_schema_identity != TICKET06_ACCEPTANCE_RESULT_SCHEMA_IDENTITY:
            raise ValueError("Ticket 06 result schema identity mismatch")
        expected_ledger = build_ticket06_command_ledger(
            self.static.environment_policy_identity
        )
        if expected_ledger.ledger_identity != self.static.command_ledger_identity:
            raise ValueError("Ticket 06 result ledger identity mismatch")
        if tuple((item.command_id, item.ordinal) for item in self.command_results) != tuple(
            (entry.command_id, entry.ordinal) for entry in expected_ledger.entries
        ):
            raise ValueError("Ticket 06 command results are incomplete or unordered")
        failure_seen = False
        for entry, result in zip(
            expected_ledger.entries,
            self.command_results,
            strict=True,
        ):
            if (
                result.result_category is not entry.result_category
                or result.mandatory != entry.mandatory
                or result.parser_identity
                != entry.expected_result_parser.parser_identity
            ):
                raise ValueError("Ticket 06 command result binding mismatch")
            if failure_seen:
                if result.status is not AcceptanceCommandStatus.NOT_RUN:
                    raise ValueError("commands ran after a mandatory failure")
                continue
            if result.status is AcceptanceCommandStatus.NOT_RUN:
                raise ValueError("command was omitted before a mandatory failure")
            if result.status in {
                AcceptanceCommandStatus.FAIL,
                AcceptanceCommandStatus.TIMEOUT,
            }:
                failure_seen = True
        all_pass = all(
            item.status is AcceptanceCommandStatus.PASS
            for item in self.command_results
        )
        if (self.guest_claim == "pass") is not all_pass:
            raise ValueError("guest claim contradicts mandatory command results")
        if tuple(item.criterion_id for item in self.qcf) != tuple(
            f"QCF{number}" for number in range(1, 19)
        ):
            raise ValueError("QCF evidence is incomplete or unordered")
        if tuple(item.criterion_id for item in self.pe) != tuple(
            f"PE{number}" for number in range(1, 16)
        ):
            raise ValueError("PE evidence is incomplete or unordered")
        if tuple(item.criterion_id for item in self.ac) != tuple(
            f"AC{number}" for number in range(1, 14)
        ):
            raise ValueError("AC evidence is incomplete or unordered")
        if self.authority.qcf17_guest_clause_a is not self.qcf[16].evidence_status:
            raise ValueError("QCF17 guest clause A evidence mismatch")
        if self.authority.qcf18_guest_evidence is not self.qcf[17].evidence_status:
            raise ValueError("QCF18 guest evidence mismatch")
        summaries, qcf, pe, ac, overall_status = _derive_acceptance_evidence(
            self.command_results
        )
        if (
            self.category_summaries != summaries
            or self.qcf != qcf
            or self.pe != pe
            or self.ac != ac
            or self.authority.qcf17_guest_clause_a is not overall_status
            or self.authority.qcf18_guest_evidence is not overall_status
        ):
            raise ValueError("Ticket 06 evidence summary is contradictory")
        payload = self.model_dump(mode="json", exclude={"result_identity"})
        expected = content_identity("windows-sandbox-ticket06-result:v1", payload)
        if self.result_identity != expected:
            raise ValueError("Ticket 06 result identity mismatch")
        return self

    def canonical_json_bytes(self) -> bytes:
        return canonical_json_bytes(self.model_dump(mode="json"))


def _evidence_status(results: tuple[Ticket06CommandResult, ...]) -> AcceptanceEvidenceStatus:
    if any(
        item.status in {AcceptanceCommandStatus.FAIL, AcceptanceCommandStatus.TIMEOUT}
        for item in results
    ):
        return AcceptanceEvidenceStatus.FAIL
    if results and all(item.status is AcceptanceCommandStatus.PASS for item in results):
        return AcceptanceEvidenceStatus.PASS
    return AcceptanceEvidenceStatus.NOT_RUN


def _criterion(
    criterion_id: str,
    status: AcceptanceEvidenceStatus,
    command_ids: tuple[str, ...],
) -> AcceptanceCriterionResult:
    payload = {
        "criterion_id": criterion_id,
        "evidence_status": status,
        "command_ids": list(command_ids),
    }
    return AcceptanceCriterionResult(
        **payload,
        evidence_identity=content_identity("ticket06-criterion-evidence:v1", payload),
    )


def _category_summary(
    category: AcceptanceResultCategory,
    results: tuple[Ticket06CommandResult, ...],
) -> AcceptanceCategorySummary:
    payload = {
        "category": category,
        "status": _evidence_status(results),
        "command_ids": [item.command_id for item in results],
    }
    return AcceptanceCategorySummary(
        **payload,
        summary_identity=content_identity("ticket06-category-summary:v1", payload),
    )


def _derive_acceptance_evidence(
    command_results: tuple[Ticket06CommandResult, ...],
) -> tuple[
    tuple[AcceptanceCategorySummary, ...],
    tuple[AcceptanceCriterionResult, ...],
    tuple[AcceptanceCriterionResult, ...],
    tuple[AcceptanceCriterionResult, ...],
    AcceptanceEvidenceStatus,
]:
    by_category = {
        category: tuple(
            result for result in command_results if result.result_category is category
        )
        for category in AcceptanceResultCategory
    }
    summaries = tuple(
        _category_summary(category, by_category[category])
        for category in AcceptanceResultCategory
        if by_category[category]
    )
    qcf_commands = by_category[AcceptanceResultCategory.QCF]
    qcf_status = _evidence_status(qcf_commands)
    overall_status = _evidence_status(command_results)
    qcf = tuple(
        _criterion(
            f"QCF{number}",
            overall_status if number in {17, 18} else qcf_status,
            tuple(
                item.command_id
                for item in (command_results if number in {17, 18} else qcf_commands)
            ),
        )
        for number in range(1, 19)
    )
    pe_commands = by_category[AcceptanceResultCategory.PE]
    pe_status = _evidence_status(pe_commands)
    pe = tuple(
        _criterion(
            f"PE{number}",
            pe_status,
            tuple(item.command_id for item in pe_commands),
        )
        for number in range(1, 16)
    )
    ac_commands = by_category[AcceptanceResultCategory.AC]
    ac_status = _evidence_status(ac_commands)
    ac = tuple(
        _criterion(
            f"AC{number}",
            ac_status,
            tuple(item.command_id for item in ac_commands),
        )
        for number in range(1, 14)
    )
    return summaries, qcf, pe, ac, overall_status


def build_ticket06_acceptance_result(
    *,
    static: Ticket06AcceptanceStaticBindings,
    ledger: Ticket06CommandLedger,
    command_results: tuple[Ticket06CommandResult, ...],
    live_environment: tuple[tuple[str, str], ...],
) -> Ticket06AcceptanceResult:
    if (
        ledger.ledger_identity != static.command_ledger_identity
        or ledger
        != build_ticket06_command_ledger(static.environment_policy_identity)
    ):
        raise Ticket06AcceptanceContractError(
            Ticket06AcceptanceFailureReason.INVALID_LEDGER
        )
    summaries, qcf, pe, ac, overall_status = _derive_acceptance_evidence(
        command_results
    )
    no_network_payload = {
        "networking_disabled": True,
        "ledger_network_commands_absent": True,
        "live_environment": [list(item) for item in live_environment],
        "dotenv_disabled": True,
        "credentials_present": False,
        "provider_commands_executed": False,
        "model_commands_executed": False,
    }
    no_network = NoNetworkNoLiveEvidence(
        **no_network_payload,
        evidence_identity=content_identity(
            "ticket06-no-network-no-live:v1",
            no_network_payload,
        ),
    )
    all_pass = all(
        item.status is AcceptanceCommandStatus.PASS for item in command_results
    )
    payload = {
        "contract_version": TICKET06_ACCEPTANCE_RESULT_VERSION,
        "result_schema_identity": TICKET06_ACCEPTANCE_RESULT_SCHEMA_IDENTITY,
        "static": static.model_dump(mode="json"),
        "guest_claim": "pass" if all_pass else "fail",
        "command_results": [item.model_dump(mode="json") for item in command_results],
        "category_summaries": [item.model_dump(mode="json") for item in summaries],
        "qcf": [item.model_dump(mode="json") for item in qcf],
        "pe": [item.model_dump(mode="json") for item in pe],
        "ac": [item.model_dump(mode="json") for item in ac],
        "authority": Ticket06AuthorityBoundary(
            qcf17_guest_clause_a=overall_status,
            qcf18_guest_evidence=overall_status,
        ).model_dump(mode="json"),
        "no_network_no_live": no_network.model_dump(mode="json"),
        "completion_marker_written_by_orchestrator": True,
    }
    return Ticket06AcceptanceResult(
        **payload,
        result_identity=content_identity("windows-sandbox-ticket06-result:v1", payload),
    )


class Ticket06CompletionMarker(BaseModel):
    model_config = _CLOSED_MODEL_CONFIG

    contract_version: Literal["windows-sandbox-ticket06-completion-v1"] = (
        "windows-sandbox-ticket06-completion-v1"
    )
    result_identity: str = Field(
        pattern=r"^windows-sandbox-ticket06-result:v1:sha256:[0-9a-f]{64}$"
    )
    result_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    write_order: Literal["last"] = "last"
    marker_identity: str = Field(
        pattern=r"^windows-sandbox-ticket06-completion:v1:sha256:[0-9a-f]{64}$"
    )

    @model_validator(mode="after")
    def _validate_marker(self) -> Ticket06CompletionMarker:
        payload = self.model_dump(mode="json", exclude={"marker_identity"})
        expected = content_identity("windows-sandbox-ticket06-completion:v1", payload)
        if self.marker_identity != expected:
            raise ValueError("Ticket 06 completion marker identity mismatch")
        return self

    def canonical_json_bytes(self) -> bytes:
        return canonical_json_bytes(self.model_dump(mode="json"))


def build_ticket06_completion_marker(
    *,
    result: Ticket06AcceptanceResult,
    result_bytes: bytes,
) -> Ticket06CompletionMarker:
    payload = {
        "contract_version": "windows-sandbox-ticket06-completion-v1",
        "result_identity": result.result_identity,
        "result_sha256": sha256(result_bytes).hexdigest(),
        "write_order": "last",
    }
    return Ticket06CompletionMarker(
        **payload,
        marker_identity=content_identity(
            "windows-sandbox-ticket06-completion:v1",
            payload,
        ),
    )


class ValidatedTicket06AcceptanceExport(BaseModel):
    model_config = ConfigDict(frozen=True, arbitrary_types_allowed=True)

    result: Ticket06AcceptanceResult
    completion: Ticket06CompletionMarker
    export_identity: str = Field(pattern=_IDENTITY_PATTERN)
    host_qcf17_authority: Literal[False] = False
    host_qcf18_authority: Literal[False] = False


_UNSAFE_EXPORT_PATTERN = re.compile(
    rb"(?:-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----|"
    rb"\bAKIA[0-9A-Z]{16}\b|\bgh[oprsu]_[A-Za-z0-9]{20,}\b|"
    rb"\bsk-[A-Za-z0-9_-]{20,}\b|https?://[^\s/:@]+:[^\s/@]+@|"
    rb"[A-Za-z]:\\Users\\)",
    flags=re.IGNORECASE,
)


def validate_ticket06_acceptance_export(
    export_root: Path,
    *,
    expected: Ticket06AcceptanceStaticBindings,
) -> ValidatedTicket06AcceptanceExport:
    try:
        root = export_root.resolve(strict=True)
        files = tuple(sorted(root.iterdir(), key=lambda item: item.name))
    except OSError as exc:
        raise Ticket06AcceptanceContractError(
            Ticket06AcceptanceFailureReason.UNSAFE_EXPORT
        ) from exc
    if not root.is_dir() or tuple(item.name for item in files) != (
        "completion.json",
        "result.json",
    ):
        raise Ticket06AcceptanceContractError(
            Ticket06AcceptanceFailureReason.UNSAFE_EXPORT
        )
    total = 0
    for path in files:
        try:
            metadata = path.lstat()
        except OSError as exc:
            raise Ticket06AcceptanceContractError(
                Ticket06AcceptanceFailureReason.UNSAFE_EXPORT
            ) from exc
        if (
            not stat.S_ISREG(metadata.st_mode)
            or stat.S_ISLNK(metadata.st_mode)
            or metadata.st_size > 262_144
        ):
            raise Ticket06AcceptanceContractError(
                Ticket06AcceptanceFailureReason.UNSAFE_EXPORT
            )
        total += metadata.st_size
    if total > 524_288:
        raise Ticket06AcceptanceContractError(
            Ticket06AcceptanceFailureReason.UNSAFE_EXPORT
        )
    result_path = root / "result.json"
    completion_path = root / "completion.json"
    try:
        result_bytes = result_path.read_bytes()
        completion_bytes = completion_path.read_bytes()
        if _UNSAFE_EXPORT_PATTERN.search(result_bytes + completion_bytes):
            raise Ticket06AcceptanceContractError(
                Ticket06AcceptanceFailureReason.UNSAFE_EXPORT
            )
        result = Ticket06AcceptanceResult.model_validate_json(result_bytes)
        completion = Ticket06CompletionMarker.model_validate_json(completion_bytes)
    except Ticket06AcceptanceContractError:
        raise
    except (OSError, TypeError, ValueError) as exc:
        raise Ticket06AcceptanceContractError(
            Ticket06AcceptanceFailureReason.INVALID_COMPLETION_MARKER
        ) from exc
    if result.static != expected:
        raise Ticket06AcceptanceContractError(
            Ticket06AcceptanceFailureReason.RESULT_IDENTITY_MISMATCH
        )
    if (
        completion.result_identity != result.result_identity
        or completion.result_sha256 != sha256(result_bytes).hexdigest()
        or completion_path.stat().st_mtime_ns < result_path.stat().st_mtime_ns
    ):
        raise Ticket06AcceptanceContractError(
            Ticket06AcceptanceFailureReason.INVALID_COMPLETION_MARKER
        )
    export_payload = {
        "result_identity": result.result_identity,
        "marker_identity": completion.marker_identity,
        "result_sha256": sha256(result_bytes).hexdigest(),
        "marker_sha256": sha256(completion_bytes).hexdigest(),
    }
    return ValidatedTicket06AcceptanceExport(
        result=result,
        completion=completion,
        export_identity=content_identity(
            "windows-sandbox-ticket06-result-export:v1",
            export_payload,
        ),
    )


def _python_function(tree: ast.Module, name: str) -> ast.FunctionDef:
    matches = [
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.FunctionDef) and node.name == name
    ]
    if len(matches) != 1:
        raise ValueError(f"reviewed Python function is not closed: {name}")
    return matches[0]


def _call_name(node: ast.AST) -> str | None:
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        prefix = _call_name(node.value)
        return f"{prefix}.{node.attr}" if prefix else node.attr
    return None


def _function_calls(function: ast.AST) -> tuple[tuple[int, str], ...]:
    calls = []
    for node in ast.walk(function):
        if not isinstance(node, ast.Call):
            continue
        name = _call_name(node.func)
        if name is not None:
            calls.append((node.lineno, name))
    return tuple(sorted(calls))


def _unique_call_line(
    calls: tuple[tuple[int, str], ...],
    expected_suffix: str,
) -> int:
    lines = [line for line, name in calls if name.endswith(expected_suffix)]
    if len(lines) != 1:
        raise ValueError(f"reviewed call path is not closed: {expected_suffix}")
    return lines[0]


def _validate_acceptance_orchestrator_write_order(source: str) -> None:
    tree = ast.parse(source)
    function = _python_function(tree, "execute_ticket06_acceptance")
    result_write: ast.With | None = None
    completion_write: ast.With | None = None
    for node in ast.walk(function):
        if not isinstance(node, ast.With) or len(node.items) != 1:
            continue
        context = node.items[0].context_expr
        if (
            not isinstance(context, ast.Call)
            or _call_name(context.func) not in {"result_path.open", "completion_path.open"}
            or len(context.args) != 1
            or not isinstance(context.args[0], ast.Constant)
            or context.args[0].value != "xb"
        ):
            continue
        if _call_name(context.func) == "result_path.open":
            result_write = node
        else:
            completion_write = node
    if result_write is None or completion_write is None:
        raise ValueError("acceptance export does not use exclusive bounded writes")
    result_calls = _function_calls(result_write)
    completion_calls = _function_calls(completion_write)
    if (
        result_write.lineno >= completion_write.lineno
        or len([name for _line, name in result_calls if name == "handle.write"]) != 1
        or len([name for _line, name in result_calls if name == "os.fsync"]) != 1
        or len([name for _line, name in completion_calls if name == "handle.write"]) != 1
        or len([name for _line, name in completion_calls if name == "os.fsync"]) != 1
    ):
        raise ValueError("acceptance completion marker is not the durable last write")
    completion_end = completion_write.end_lineno or completion_write.lineno
    forbidden_after_completion = {
        "open",
        "write_bytes",
        "write_text",
        "replace",
        "rename",
        "unlink",
    }
    if any(
        line > completion_end and name.rsplit(".", maxsplit=1)[-1] in forbidden_after_completion
        for line, name in _function_calls(function)
    ):
        raise ValueError("acceptance completion marker is not last-write")


def _validate_host_acceptance_paths(source: str) -> None:
    tree = ast.parse(source)
    construct_calls = _function_calls(
        _python_function(tree, "construct_ticket06_acceptance")
    )
    construct_sequence = (
        _unique_call_line(construct_calls, "self.construct"),
        _unique_call_line(construct_calls, "_mapping_bindings"),
        _unique_call_line(construct_calls, "build_mapping_manifest"),
        _unique_call_line(construct_calls, "render_wsb_configuration"),
        _unique_call_line(
            construct_calls,
            "build_ticket06_acceptance_control_envelope",
        ),
        _unique_call_line(construct_calls, "self._revalidate_ticket06_contract"),
    )
    launch_calls = _function_calls(
        _python_function(tree, "launch_ticket06_acceptance")
    )
    launch_sequence = (
        _unique_call_line(launch_calls, "self._revalidate_ticket06_contract"),
        _unique_call_line(launch_calls, "self._platform.launch"),
        _unique_call_line(
            launch_calls,
            "persist_iso04_sandbox_process_evidence",
        ),
        _unique_call_line(launch_calls, "process_evidence.to_process_result"),
        _unique_call_line(launch_calls, "validate_ticket06_acceptance_export"),
    )
    report_calls = _function_calls(
        _python_function(tree, "write_iso04_ticket06_report")
    )
    report_sequence = (
        _unique_call_line(
            report_calls,
            "persist_iso04_ticket06_operational_evidence",
        ),
        _unique_call_line(report_calls, "report_builder"),
        _unique_call_line(
            report_calls,
            "ISO04Ticket06AcceptanceReport.model_validate",
        ),
        _unique_call_line(report_calls, "_write_host_audit"),
    )
    forbidden_mapping_calls = {
        name
        for _line, name in construct_calls
        if name.endswith("SandboxMappingBinding")
    }
    if (
        tuple(sorted(construct_sequence)) != construct_sequence
        or tuple(sorted(launch_sequence)) != launch_sequence
        or tuple(sorted(report_sequence)) != report_sequence
        or forbidden_mapping_calls
    ):
        raise ValueError("acceptance mapping/export validation path is not closed")


def qualify_ticket06_acceptance_runner_structure(
    runner_root: Path,
    *,
    expected_runner_identity: str,
    base_environment_variables: tuple[tuple[str, str], ...],
    mapping_manifest: ResolvedSandboxMappingManifest,
    wsb_configuration: WsbConfiguration,
    boundary_runner_identity: str,
    host_harness_manifest_identity: str,
    host_acceptance_module_path: Path,
    expected_host_acceptance_module_sha256: str,
) -> Ticket06AcceptanceStructuralQualification:
    """Qualify the sealed acceptance mode without executing a Ticket 06 command."""

    try:
        root = runner_root.resolve(strict=True)
        manifest = validate_qualified_runner_input(
            root,
            expected_identity=expected_runner_identity,
        )
        bootstrap = (root / "bootstrap.ps1").read_text(encoding="utf-8")
        orchestrator = (root / "run_ticket06_acceptance.py").read_text(
            encoding="utf-8"
        )
        offline_review = (root / "run_ticket06_offline_review.py").read_text(
            encoding="utf-8"
        )
        sealed_contract = (root / "ticket06_acceptance_contract.py").read_bytes()
        if sealed_contract != Path(__file__).resolve(strict=True).read_bytes():
            raise ValueError("sealed acceptance contract differs from reviewed host contract")

        boundary_dispatch = 'if ($executionMode -eq "boundary_qualification")'
        acceptance_dispatch = 'elseif ($executionMode -eq "ticket06_acceptance")'
        if (
            bootstrap.count(boundary_dispatch) != 1
            or bootstrap.count(acceptance_dispatch) != 1
            or '"boundary_qualification"' not in bootstrap
            or '"ticket06_acceptance"' not in bootstrap
            or "$control.base.command" in bootstrap
            or "$control.base.script" in bootstrap
        ):
            raise ValueError("bootstrap execution-mode dispatch is not closed")
        boundary_block, acceptance_tail = bootstrap.split(
            boundary_dispatch,
            maxsplit=1,
        )[1].split(
            acceptance_dispatch,
            maxsplit=1,
        )
        acceptance_block = acceptance_tail.split('$stage = "completed"', maxsplit=1)[0]
        boundary_target = r"C:\QCF17\Input\Runner\run_boundary_qualification.py"
        acceptance_target = r"C:\QCF17\Input\Runner\run_ticket06_acceptance.py"
        if (
            boundary_target not in boundary_block
            or acceptance_target in boundary_block
            or acceptance_block.count(acceptance_target) != 1
            or boundary_target in acceptance_block
        ):
            raise ValueError("bootstrap dispatch target is not fixed by execution mode")
        if (
            "from ticket06_acceptance_contract import" not in orchestrator
            or "execute_ticket06_acceptance" not in orchestrator
            or "from ticket06_acceptance_contract import" not in offline_review
            or "run_offline_review" not in offline_review
            or re.search(
                r"ticket_?07",
                bootstrap + orchestrator + offline_review,
                flags=re.IGNORECASE,
            )
        ):
            raise ValueError("acceptance executable contract imports are not closed")
        _validate_acceptance_orchestrator_write_order(orchestrator)

        host_source_path = host_acceptance_module_path.resolve(strict=True)
        host_metadata = host_source_path.lstat()
        if (
            not stat.S_ISREG(host_metadata.st_mode)
            or stat.S_ISLNK(host_metadata.st_mode)
            or host_metadata.st_size > 4 * 1024 * 1024
        ):
            raise ValueError("reviewed host acceptance module is unsafe")
        host_source_bytes = host_source_path.read_bytes()
        host_source_sha256 = sha256(host_source_bytes).hexdigest()
        if host_source_sha256 != expected_host_acceptance_module_sha256:
            raise ValueError("reviewed host acceptance module identity mismatch")
        _validate_host_acceptance_paths(host_source_bytes.decode("utf-8"))

        from tradingagents.windows_sandbox_runner import (
            SandboxMappingRole,
            validate_wsb_configuration,
        )

        validate_wsb_configuration(
            wsb_configuration.xml,
            mapping_manifest=mapping_manifest,
            protected_client_supported=(
                wsb_configuration.policy.protected_client_supported
            ),
        )
        mappings = mapping_manifest.contract.mappings
        if (
            wsb_configuration.config_identity
            != content_identity("windows-sandbox-config:v1", wsb_configuration.xml)
            or wsb_configuration.policy.networking != "Disable"
            or tuple(item.role for item in mappings) != tuple(SandboxMappingRole)
            or any(not item.read_only for item in mappings[:-1])
            or mappings[-1].role is not SandboxMappingRole.RESULTS_EXPORT
            or mappings[-1].read_only
            or not mappings[-1].fresh_empty
            or sum(not item.read_only for item in mappings) != 1
        ):
            raise ValueError("acceptance mapping/network structure is not closed")

        environment = build_ticket06_control_environment(base_environment_variables)
        ledger = build_ticket06_command_ledger(environment.policy_identity)
        serialized_ledger = ledger.canonical_json_bytes().decode("utf-8").casefold()
        full_suite_a = tuple(
            entry
            for entry in ledger.entries
            if entry.result_category is AcceptanceResultCategory.FULL_SUITE_A
        )
        full_suite_b = tuple(
            entry
            for entry in ledger.entries
            if entry.result_category is AcceptanceResultCategory.FULL_SUITE_B
        )
        if (
            ledger.ledger_identity != TICKET06_QUALIFIED_COMMAND_LEDGER_IDENTITY
            or len(ledger.entries) != 27
            or not all(entry.mandatory for entry in ledger.entries)
            or len(full_suite_a) != 1
            or len(full_suite_b) != 1
            or full_suite_a[0].command_id == full_suite_b[0].command_id
            or any(
                forbidden in serialized_ledger
                for forbidden in (
                    "ticket07",
                    "ticket_07",
                    "qualified_v1",
                    "http://",
                    "https://",
                )
            )
        ):
            raise ValueError("Ticket 06 command ledger is not the qualified closed ledger")

        def identity(namespace: str, fill: str) -> str:
            return f"{namespace}:sha256:{fill * 64}"

        sample_static = Ticket06AcceptanceStaticBindings(
            source_commit_id="1" * 40,
            source_tree_hash="2" * 40,
            source_input_identity=identity("windows-sandbox-reviewed-source:v1", "3"),
            source_manifest_identity=identity("windows-sandbox-source-manifest:v1", "4"),
            source_archive_sha256="5" * 64,
            reconstructed_source_tree_identity=identity(
                "windows-sandbox-reconstructed-source:v1", "6"
            ),
            runtime_bundle_identity=identity("windows-offline-runtime-bundle:v1", "7"),
            runtime_bundle_manifest_sha256="8" * 64,
            runtime_bundle_contract_identity=identity(
                "windows-sandbox-runtime-bundle-input:v1", "9"
            ),
            runner_identity=identity("iso01-windows-sandbox-runner:v1", "a"),
            runner_input_identity=manifest.runner_input_identity,
            wsb_config_identity=identity("windows-sandbox-config:v1", "b"),
            protected_root_manifest_identity=identity(
                "windows-sandbox-protected-roots:v1", "c"
            ),
            mapping_manifest_identity=identity(
                "windows-sandbox-mapping-manifest:v1", "d"
            ),
            guest_workspace_identity=identity(
                "windows-sandbox-guest-workspace:v1", "e"
            ),
            environment_policy_identity=environment.policy_identity,
            control_identity=identity("windows-sandbox-ticket06-control:v1", "f"),
            frozen_baseline_identity=identity("ticket06-frozen-baseline:v1", "1"),
            boundary_qualification_identity=identity(
                "iso03-boundary-qualification-run:v1", "2"
            ),
            command_ledger_identity=ledger.ledger_identity,
        )
        sample_results = tuple(
            build_ticket06_command_result(
                entry,
                status=(
                    AcceptanceCommandStatus.FAIL
                    if entry.ordinal == 1
                    else AcceptanceCommandStatus.NOT_RUN
                ),
                returncode=1 if entry.ordinal == 1 else None,
                failed=1 if entry.ordinal == 1 else 0,
            )
            for entry in ledger.entries
        )
        sample_result = build_ticket06_acceptance_result(
            static=sample_static,
            ledger=ledger,
            command_results=sample_results,
            live_environment=environment.variables,
        )
        result_bytes = sample_result.canonical_json_bytes()
        Ticket06AcceptanceResult.model_validate_json(result_bytes)
        sample_marker = build_ticket06_completion_marker(
            result=sample_result,
            result_bytes=result_bytes,
        )
        if (
            Ticket06CompletionMarker.model_validate_json(
                sample_marker.canonical_json_bytes()
            ).write_order
            != "last"
        ):
            raise ValueError("Ticket 06 completion marker is not last-write")
    except Ticket06AcceptanceContractError:
        raise
    except (OSError, TypeError, ValueError) as exc:
        raise Ticket06AcceptanceContractError(
            Ticket06AcceptanceFailureReason.INVALID_CONTROL
        ) from exc

    return Ticket06AcceptanceStructuralQualification.create(
        runner_input_identity=manifest.runner_input_identity,
        environment_policy_identity=environment.policy_identity,
        host_harness_manifest_identity=host_harness_manifest_identity,
        host_acceptance_module_sha256=host_source_sha256,
        boundary_runner_identity=boundary_runner_identity,
        wsb_config_identity=wsb_configuration.config_identity,
        wsb_config_policy_identity=wsb_configuration.policy.policy_identity,
        mapping_manifest_identity=mapping_manifest.mapping_manifest_identity,
        command_ledger_identity=ledger.ledger_identity,
        result_schema_identity=TICKET06_ACCEPTANCE_RESULT_SCHEMA_IDENTITY,
    )

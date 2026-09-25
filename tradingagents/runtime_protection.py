"""Versioned, offline evidence for a single Windows protection attempt.

These contracts do not qualify source, allocate an analysis run, or authorize
execution. The tracked Windows host owns fresh filesystem objects and the
verified-bytes-to-child-exit protection interval. Historical attempts are input
evidence only; there is deliberately no recovery or reset operation.
"""

from __future__ import annotations

import json
import re
from collections.abc import Mapping, Sequence
from datetime import date
from hashlib import sha256
from pathlib import PureWindowsPath
from typing import Annotated, Any, ClassVar, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from tradingagents.policy_qualified_contract import DownstreamEnvelope, strict_json
from tradingagents.policy_qualified_results import PolicyQualifiedVerifier

_CLOSED = ConfigDict(extra="forbid", frozen=True, strict=True)
Digest = Annotated[str, Field(pattern=r"^[0-9a-f]{64}$")]
Identity = Annotated[str, Field(pattern=r"^[a-z0-9][a-z0-9:-]*:v[1-9][0-9]*:sha256:[0-9a-f]{64}$")]
CONTRACT_VERSION = "runtime-protection-attempt-v1"


def canonical_bytes(value: object) -> bytes:
    return json.dumps(
        value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False
    ).encode("utf-8")


def evidence_identity(namespace: str, value: object) -> str:
    return f"{namespace}:sha256:{sha256(canonical_bytes(value)).hexdigest()}"


def _windows_path(value: str) -> str:
    path = PureWindowsPath(value)
    if (
        not re.match(r"^[A-Z]:\\[^\\]", value)
        or "/" in value
        or value.endswith("\\")
        or str(path) != value
        or any(part in (".", "..") or part.endswith((".", " ")) for part in path.parts[1:])
        or any(char in value[2:] for char in ':*?"<>|')
        or any(ord(char) < 32 for char in value)
        or any(PureWindowsPath(part).is_reserved() for part in path.parts[1:])
    ):
        raise ValueError("namespace must be a canonical absolute local Windows path")
    return value


class AttemptInputs(BaseModel):
    """All semantic inputs, including an explicit sealed ordinal, select the root.

    An ordinal is an allocation discriminator, never an analysis thread nonce.
    Repeating these inputs is an attempt to reuse the same claim and must fail.
    """

    # A conditional subclass must not enter a legacy consumer as an already
    # validated base instance. Revalidate its discriminant and complete fields.
    model_config = ConfigDict(**_CLOSED, revalidate_instances="always")
    contract_version: Literal["runtime-protection-attempt-v1"] = CONTRACT_VERSION
    namespace_root: str
    namespace_identity: str = Field(
        pattern=r"^runtime-protection-namespace:v1:sha256:[0-9a-f]{64}$"
    )
    source_commit: str = Field(pattern=r"^[0-9a-f]{40}$")
    source_tree: str = Field(pattern=r"^[0-9a-f]{40}$")
    qualified_source_identity: str = Field(
        pattern=r"^qualified-source-ready-for-ticket07-preparation:v1:sha256:[0-9a-f]{64}$"
    )
    production_preparation_identity: str = Field(
        pattern=r"^production-qualified-routing-preparation:v1:[0-9a-f]{64}$"
    )
    preparation_lineage_identity: Identity
    completed_session: str
    prospective_thread: str = Field(pattern=r"^[0-9a-f]{16}$")
    prospective_run: str = Field(pattern=r"^run:[0-9a-f]{64}$")
    runtime_content_identity: Identity
    app_content_identity: Identity
    runtime_ancestry_identity: Identity
    app_ancestry_identity: Identity
    deployment_configuration_identity: Identity
    environment_identity: Identity
    protection_source_identity: str = Field(
        pattern=r"^runtime-protection-source:v1:sha256:[0-9a-f]{64}$"
    )
    attempt_ordinal: int = Field(gt=0, strict=True)

    _validate_root = field_validator("namespace_root")(_windows_path)

    @field_validator("completed_session")
    @classmethod
    def _session(cls, value: str) -> str:
        if date.fromisoformat(value).isoformat() != value:
            raise ValueError("completed session must be an ISO calendar date")
        return value


class AttemptPaths(BaseModel):
    model_config = _CLOSED
    attempt_root: str
    payload_root: str
    private_runtime_root: str
    private_app_root: str
    current_anchor: str
    history_anchor: str
    evidence_root: str
    claim_root: str


class ConditionalAttemptInputs(AttemptInputs):
    """New namespace and inseparable caveat; legacy native hosts reject this."""

    contract_version: Literal["policy-qualified-runtime-protection-attempt-v1"]
    qualified_source_identity: str = Field(
        pattern=r"^policy-qualified-execution-result:v1:sha256:[0-9a-f]{64}$"
    )
    production_preparation_identity: str = Field(
        pattern=r"^production-policy-qualified-routing-preparation:v1:[0-9a-f]{64}$"
    )
    acceptance: DownstreamEnvelope

    @model_validator(mode="after")
    def _conditional_binding(self) -> ConditionalAttemptInputs:
        core = self.acceptance.core
        if (
            self.source_commit != core.candidate.commit
            or self.source_tree != core.candidate.tree
            or self.qualified_source_identity != core.result.identity
        ):
            raise ValueError("attempt lost the conditional result/candidate binding")
        return self


def _paths(inputs: AttemptInputs) -> tuple[str, AttemptPaths]:
    namespace_name = (
        "policy-qualified-runtime-protection-attempt:v1"
        if isinstance(inputs, ConditionalAttemptInputs)
        else "runtime-protection-attempt:v1"
    )
    identity = evidence_identity(namespace_name, inputs.model_dump(mode="json"))
    digest = identity.rsplit(":", 1)[1]
    namespace = PureWindowsPath(inputs.namespace_root)
    root = namespace / "attempts" / digest
    payload = root / "payload"
    runtime = payload / "runtime"
    return identity, AttemptPaths(
        attempt_root=str(root),
        payload_root=str(payload),
        private_runtime_root=str(runtime),
        private_app_root=str(runtime / "app"),
        current_anchor=str(payload / "anchors" / "current"),
        history_anchor=str(payload / "anchors" / "history"),
        evidence_root=str(root / "evidence"),
        claim_root=str(namespace / "claims" / digest),
    )


class ProtectionAttempt(BaseModel):
    model_config = _CLOSED
    contract_version: Literal["runtime-protection-attempt-v1"] = CONTRACT_VERSION
    inputs: AttemptInputs
    attempt_identity: Identity
    paths: AttemptPaths
    execution_authorized: Literal[False] = False

    @model_validator(mode="after")
    def _binding(self) -> ProtectionAttempt:
        identity, paths = _paths(self.inputs)
        if self.attempt_identity != identity or self.paths != paths:
            raise ValueError("attempt identity/path binding mismatch")
        return self


class ConditionalProtectionAttempt(BaseModel):
    model_config = _CLOSED
    contract_version: Literal["policy-qualified-runtime-protection-attempt-v1"]
    inputs: ConditionalAttemptInputs
    attempt_identity: Identity
    paths: AttemptPaths
    execution_authorized: Literal[False] = False

    @model_validator(mode="after")
    def _binding(self) -> ConditionalProtectionAttempt:
        identity, paths = _paths(self.inputs)
        if self.attempt_identity != identity or self.paths != paths:
            raise ValueError("conditional attempt identity/path binding mismatch")
        return self


def derive_attempt(
    inputs: AttemptInputs,
    *,
    policy_verifier: PolicyQualifiedVerifier | None = None,
) -> ProtectionAttempt | ConditionalProtectionAttempt:
    """Derive paths; conditional inputs first revalidate owner-backed evidence.

    Legacy derivation remains pure. Neither branch creates attempt paths or
    launches anything; conditional expiry/revocation may invalidate its ledger.
    """
    if isinstance(inputs, ConditionalAttemptInputs):
        inputs = ConditionalAttemptInputs.model_validate_json(
            canonical_bytes(inputs.model_dump(mode="json"))
        )
        if not isinstance(policy_verifier, PolicyQualifiedVerifier):
            raise ValueError("conditional attempt requires fresh trusted authority resolution")
        policy_verifier.verify_downstream(inputs.acceptance)
        identity, paths = _paths(inputs)
        return ConditionalProtectionAttempt(
            contract_version="policy-qualified-runtime-protection-attempt-v1",
            inputs=inputs,
            attempt_identity=identity,
            paths=paths,
        )
    inputs = AttemptInputs.model_validate(inputs.model_dump(mode="json"))
    identity, paths = _paths(inputs)
    return ProtectionAttempt(inputs=inputs, attempt_identity=identity, paths=paths)


def bind_production_preparation(
    preparation: object,
    *,
    policy_verifier: PolicyQualifiedVerifier | None = None,
    **inputs: Any,
) -> ProtectionAttempt | ConditionalProtectionAttempt:
    """Add attempt identity to the existing production configuration seal.

    The qualified-source equality and Plan/CNINFO policy checks stay owned by
    the existing preparation interface. This adapter adds no execution grant.
    """
    from tradingagents.qualified_routing_composition import (
        ConditionalProductionQualifiedRoutingPreparation,
        parse_production_preparation,
        verify_conditional_production_source,
    )

    prepared = parse_production_preparation(preparation)
    source = prepared.qualified_source
    if isinstance(prepared, ConditionalProductionQualifiedRoutingPreparation):
        verify_conditional_production_source(source, policy_verifier)
        return derive_attempt(
            ConditionalAttemptInputs.model_validate_json(
                canonical_bytes(
                    {
                        **inputs,
                        "contract_version": "policy-qualified-runtime-protection-attempt-v1",
                        "source_commit": source.commit_id,
                        "source_tree": source.tree_hash,
                        "qualified_source_identity": source.qualified_source_identity,
                        "production_preparation_identity": prepared.preparation_identity,
                        "acceptance": source.acceptance.model_dump(mode="json"),
                    }
                )
            ),
            policy_verifier=policy_verifier,
        )
    return derive_attempt(
        AttemptInputs(
            **inputs,
            source_commit=source.commit_id,
            source_tree=source.tree_hash,
            qualified_source_identity=source.qualified_source_identity,
            production_preparation_identity=prepared.preparation_identity,
        )
    )


def validate_conditional_attempt(
    value: object,
    *,
    policy_verifier: PolicyQualifiedVerifier | None = None,
) -> ConditionalProtectionAttempt:
    """Revalidate serialized contract support; this never permits a native launch."""
    if isinstance(value, BaseModel):
        value = value.model_dump(mode="json")
    if isinstance(value, (bytes, str)):
        value = strict_json(value)
    attempt = ConditionalProtectionAttempt.model_validate_json(canonical_bytes(value))
    expected = derive_attempt(attempt.inputs, policy_verifier=policy_verifier)
    if attempt != expected:
        raise ValueError("conditional attempt does not match its complete current binding")
    return attempt


def _relative(value: str) -> str:
    if (
        not value
        or "\\" in value
        or any(char in value for char in ':*?"<>|')
        or any(ord(char) < 32 for char in value)
        or any(part in ("", ".", "..") or part.endswith((".", " ")) for part in value.split("/"))
        or any(PureWindowsPath(part).is_reserved() for part in value.split("/"))
    ):
        raise ValueError("invalid content-relative path")
    return value


class ContentFile(BaseModel):
    model_config = _CLOSED
    relative_path: str
    byte_length: int = Field(ge=0, strict=True)
    sha256: Digest
    _validate_relative = field_validator("relative_path")(_relative)


class ContentManifest(BaseModel):
    model_config = _CLOSED
    contract_version: Literal["runtime-protection-content-v1"]
    role: Literal["runtime", "app"]
    source_commit: str = Field(pattern=r"^[0-9a-f]{40}$")
    source_tree: str = Field(pattern=r"^[0-9a-f]{40}$")
    ancestry_identity: Identity
    files: tuple[ContentFile, ...]
    native_images: tuple[str, ...]
    python_version: str | None
    identity: Identity

    @model_validator(mode="after")
    def _closed_inventory(self) -> ContentManifest:
        paths = [row.relative_path for row in self.files]
        if paths != sorted(paths) or len({path.casefold() for path in paths}) != len(paths):
            raise ValueError("content paths must be sorted and uniquely cased")
        if self.native_images != tuple(sorted(set(self.native_images))) or not set(
            self.native_images
        ) <= set(paths):
            raise ValueError("native image inventory is not closed")
        if any(path.lower().endswith((".pyc", ".pyo", ".pth", "._pth")) for path in paths):
            raise ValueError("bytecode and unbound search-path files are forbidden")
        if self.role == "runtime":
            if not self.python_version or not re.fullmatch(
                r"3\.[0-9]+\.[0-9]+", self.python_version
            ):
                raise ValueError("runtime requires an explicit Python version")
            if any(
                not path.startswith(("base/", "venv/")) or path == "venv/pyvenv.cfg"
                for path in paths
            ):
                raise ValueError("runtime content must have closed base/venv ancestry")
        elif self.python_version is not None:
            raise ValueError("application content does not declare interpreter ancestry")
        if self.identity != evidence_identity(
            "runtime-protection-content:v1", self.model_dump(mode="json", exclude={"identity"})
        ):
            raise ValueError("content manifest identity mismatch")
        return self


def make_content_manifest(
    *,
    role: Literal["runtime", "app"],
    source_commit: str,
    source_tree: str,
    ancestry_identity: str,
    files: Sequence[Mapping[str, Any]],
    native_images: Sequence[str],
    python_version: str | None = None,
) -> dict[str, Any]:
    payload = {
        "contract_version": "runtime-protection-content-v1",
        "role": role,
        "source_commit": source_commit,
        "source_tree": source_tree,
        "ancestry_identity": ancestry_identity,
        "files": sorted((dict(row) for row in files), key=lambda row: row["relative_path"]),
        "native_images": sorted(native_images),
        "python_version": python_version,
    }
    return ContentManifest.model_validate_json(
        canonical_bytes(
            {
                **payload,
                "identity": evidence_identity("runtime-protection-content:v1", payload),
            }
        )
    ).model_dump(mode="json")


FORBIDDEN_ENVIRONMENT = frozenset(
    {
        "PYTHONHOME",
        "PYTHONPATH",
        "PYTHONPYCACHEPREFIX",
        "PYTHONUSERBASE",
        "PYTHONSTARTUP",
        "PYTHONINSPECT",
        "PYTHONEXECUTABLE",
        "PYTHONPLATLIBDIR",
        "__PYVENV_LAUNCHER__",
        "_PYTHON_HOST_PLATFORM",
        "LD_LIBRARY_PATH",
        "LD_PRELOAD",
        "DYLD_LIBRARY_PATH",
        "DYLD_INSERT_LIBRARIES",
        "DLL_DIRECTORY",
        "PYTHON_DLL_PATH",
        "QT_PLUGIN_PATH",
        "OPENSSL_MODULES",
        "OPENSSL_ENGINES",
        "OPENSSL_CONF",
        "TRADINGAGENTS_PROTECTION_CONTEXT",
    }
)


def _environment(value: Mapping[str, str]) -> dict[str, str]:
    result = dict(value)
    if not all(isinstance(key, str) and isinstance(item, str) for key, item in result.items()):
        raise ValueError("sealed environment must contain only string keys and values")
    if len({key.upper() for key in result}) != len(result) or any(
        key.upper() in FORBIDDEN_ENVIRONMENT
        or "=" in key
        or "\0" in key + item
        or re.search(r"(?:API_KEY|TOKEN|SECRET|PASSWORD)$", key, re.IGNORECASE)
        for key, item in result.items()
    ):
        raise ValueError("environment/search-path injection")
    if (
        result.get("TRADINGAGENTS_OUTPUT_LANGUAGE") != "English"
        or result.get("PYTHON_DOTENV_DISABLED") != "1"
    ):
        raise ValueError("sealed English/dotenv environment contract changed")
    if result.get("TRADINGAGENTS_MAINLAND_CAPABILITY_ROUTING_MODE") == "qualified_v1":
        raise ValueError("qualified_v1 authority is unavailable")
    system = _windows_path(result.get("SystemRoot", ""))
    if (
        result.get("PATH")
        != "{private_runtime_root}\\base;{private_runtime_root}\\venv\\Scripts;"
        + system
        + r"\System32"
    ):
        raise ValueError(
            "sealed PATH must contain only the new private interpreter and Windows System32"
        )
    return result


class CredentialBinding(BaseModel):
    """A sealed credential name/digest; plaintext never enters retained evidence."""

    model_config = _CLOSED
    name: str = Field(pattern=r"^[A-Z][A-Z0-9_]*(?:API_KEY|TOKEN|SECRET|PASSWORD)$")
    sha256: Digest


def seal_child_environment(
    environment: Mapping[str, str],
    *,
    working_directory: str,
    credential_bindings: Sequence[Mapping[str, str]] = (),
) -> dict[str, Any]:
    credentials = [
        CredentialBinding.model_validate(dict(row)).model_dump() for row in credential_bindings
    ]
    names = [row["name"] for row in credentials]
    if names != sorted(set(names)):
        raise ValueError("credential bindings must be sorted and unique")
    body = {
        "variables": _environment(environment),
        "working_directory": _windows_path(working_directory),
        "credential_bindings": credentials,
    }
    return {**body, "identity": evidence_identity("runtime-protection-environment:v1", body)}


def seal_materialization_plan(
    *,
    attempt: ProtectionAttempt,
    runtime_content: Mapping[str, Any],
    app_content: Mapping[str, Any],
    source_locations: Mapping[str, str],
    deployment_configuration: Mapping[str, Any],
    environment: Mapping[str, str],
    working_directory: str,
    python_version: str,
    credential_bindings: Sequence[Mapping[str, str]] = (),
) -> dict[str, Any]:
    """Build an offline input to the native host; create no objects or packet.

    Content locations supply bytes only. ACLs, Python lookup mappings and the
    three authorized deployment paths are created anew for this attempt.
    """
    attempt = ProtectionAttempt.model_validate_json(attempt.model_dump_json())
    payload = {
        "contract_version": "runtime-protection-materialization-v1",
        **_materialization_payload(
            attempt=attempt,
            runtime_content=runtime_content,
            app_content=app_content,
            source_locations=source_locations,
            deployment_configuration=deployment_configuration,
            environment=environment,
            working_directory=working_directory,
            python_version=python_version,
            credential_bindings=credential_bindings,
        ),
    }
    return {
        **payload,
        "identity": evidence_identity("runtime-protection-materialization:v1", payload),
    }


def _materialization_payload(
    *,
    attempt: ProtectionAttempt | ConditionalProtectionAttempt,
    runtime_content: Mapping[str, Any],
    app_content: Mapping[str, Any],
    source_locations: Mapping[str, str],
    deployment_configuration: Mapping[str, Any],
    environment: Mapping[str, str],
    working_directory: str,
    python_version: str,
    credential_bindings: Sequence[Mapping[str, str]],
) -> dict[str, Any]:
    """Shared content/ancestry checks after the caller validates its own version."""
    runtime = ContentManifest.model_validate_json(canonical_bytes(runtime_content))
    app = ContentManifest.model_validate_json(canonical_bytes(app_content))
    bindings = attempt.inputs
    for component, role, identity, ancestry in (
        (runtime, "runtime", bindings.runtime_content_identity, bindings.runtime_ancestry_identity),
        (app, "app", bindings.app_content_identity, bindings.app_ancestry_identity),
    ):
        if (
            component.role != role
            or component.identity != identity
            or component.ancestry_identity != ancestry
            or component.source_commit != bindings.source_commit
            or component.source_tree != bindings.source_tree
        ):
            raise ValueError("component/source/attempt ancestry mismatch")
    if runtime.python_version != python_version:
        raise ValueError("interpreter version does not match runtime ancestry")
    config = dict(deployment_configuration)
    public = _environment(environment)
    working_directory = _windows_path(working_directory)
    child_environment = seal_child_environment(
        public, working_directory=working_directory, credential_bindings=credential_bindings
    )
    if bindings.deployment_configuration_identity != evidence_identity(
        "runtime-protection-configuration:v1", config
    ):
        raise ValueError("deployment configuration identity mismatch")
    if bindings.environment_identity != child_environment["identity"]:
        raise ValueError("sealed environment identity mismatch")
    if config.get("mainland_capability_routing_mode") == "qualified_v1":
        raise ValueError("qualified_v1 authority is unavailable")
    required = {
        f"{component.role}/{row.relative_path}"
        for component in (runtime, app)
        for row in component.files
    }
    if set(source_locations) != required:
        raise ValueError("materialization must supply exactly the qualified component members")
    locations = {key: _windows_path(path) for key, path in source_locations.items()}
    app_root = PureWindowsPath(attempt.paths.private_app_root)
    mapped = {
        **config,
        "project_dir": str(app_root / "tradingagents"),
        "crypto_identity_registry_path": str(
            app_root / "tradingagents/config/crypto_identity_registry.json"
        ),
        "crypto_identity_registry_checksum_path": str(
            app_root / "tradingagents/config/crypto_identity_registry.sha256"
        ),
    }
    if not set(mapped) == set(config):
        raise ValueError("deployment configuration is missing an authorized path field")
    return {
        "attempt": attempt.model_dump(mode="json"),
        "runtime_content": runtime.model_dump(mode="json"),
        "app_content": app.model_dump(mode="json"),
        "source_locations": locations,
        "deployment_configuration": config,
        "mapped_configuration": mapped,
        "environment": public,
        "working_directory": working_directory,
        "credential_bindings": child_environment["credential_bindings"],
        "python_version": python_version,
        "execution_authorized": False,
    }


class SecurityBaseline(BaseModel):
    model_config = _CLOSED
    relative_path: str
    directory: bool
    object_identity: str = Field(pattern=r"^[0-9a-f]{8}:[0-9a-f]{16}$")
    descriptor_identity: Digest
    control_flags: int
    content_sha256: Digest | None

    @model_validator(mode="after")
    def _entry(self) -> SecurityBaseline:
        if self.relative_path:
            _relative(self.relative_path)
        if self.directory != (self.content_sha256 is None) or not self.control_flags & 0x1000:
            raise ValueError("invalid fresh security/content baseline")
        return self


class PreparedProtectionAttempt(BaseModel):
    model_config = ConfigDict(**_CLOSED, revalidate_instances="always")
    identity_namespace: ClassVar[str] = "runtime-protection-prepared:v1"
    contract_version: Literal["runtime-protection-prepared-v1"]
    attempt: ProtectionAttempt
    materialization_identity: Identity
    runtime_manifest_identity: Identity
    app_manifest_identity: Identity
    baseline: tuple[SecurityBaseline, ...]
    baseline_identity: Identity
    environment: dict[str, str]
    working_directory: str
    credential_bindings: tuple[CredentialBinding, ...]
    mapped_configuration: dict[str, Any]
    protection_contract_version: Literal["protected-runtime-tree-v3"]
    state: Literal["PREPARED"]
    execution_authorized: Literal[False]
    identity: Identity

    @model_validator(mode="after")
    def _seal(self) -> PreparedProtectionAttempt:
        inputs = self.attempt.inputs
        if (
            self.runtime_manifest_identity != inputs.runtime_content_identity
            or self.app_manifest_identity != inputs.app_content_identity
        ):
            raise ValueError("runtime/app manifest belongs to another attempt")
        if (
            seal_child_environment(
                self.environment,
                working_directory=self.working_directory,
                credential_bindings=[row.model_dump() for row in self.credential_bindings],
            )["identity"]
            != inputs.environment_identity
        ):
            raise ValueError("environment does not match attempt identity")
        paths = [row.relative_path for row in self.baseline]
        if (
            not paths
            or paths[0] != ""
            or not self.baseline[0].directory
            or len({path.casefold() for path in paths}) != len(paths)
        ):
            raise ValueError("baseline object inventory is ambiguous")
        if self.baseline_identity != evidence_identity(
            "runtime-protection-baseline:v1", [row.model_dump(mode="json") for row in self.baseline]
        ):
            raise ValueError("security baseline identity mismatch")
        if self.identity != evidence_identity(
            self.identity_namespace, self.model_dump(mode="json", exclude={"identity"})
        ):
            raise ValueError("prepared attempt identity mismatch")
        return self


def build_launch_packet(
    prepared: Mapping[str, Any], *, child_arguments: Sequence[str]
) -> dict[str, Any]:
    """Construct an inert, non-authorizing packet for the protected native host.

    The host always checks the permanent claim immediately before protection;
    this pure structure builder never infers freshness from a retained capsule.
    """
    value = PreparedProtectionAttempt.model_validate_json(canonical_bytes(prepared))
    arguments = list(child_arguments)
    if len(arguments) < 3 or arguments[:2] != ["-I", "-B"]:
        raise ValueError("sealed interpreter startup flags are required")
    app = PureWindowsPath(value.attempt.paths.private_app_root)
    _windows_path(arguments[2])
    if not PureWindowsPath(arguments[2]).is_relative_to(app) or not all(
        isinstance(arg, str) and "\0" not in arg for arg in arguments
    ):
        raise ValueError("inert entrypoint must belong to the new private application")
    payload = {
        "contract_version": "runtime-protection-packet-v1",
        "operation": "inert",
        "prepared_identity": value.identity,
        "attempt_identity": value.attempt.attempt_identity,
        "paths": value.attempt.paths.model_dump(mode="json"),
        "materialization_identity": value.materialization_identity,
        "runtime_manifest_identity": value.runtime_manifest_identity,
        "app_manifest_identity": value.app_manifest_identity,
        "baseline_identity": value.baseline_identity,
        "protection_contract_version": value.protection_contract_version,
        "child_arguments": arguments,
        "execution_authorized": False,
    }
    return {**payload, "identity": evidence_identity("runtime-protection-packet:v1", payload)}


def _checked_evidence(value: Mapping[str, Any], namespace: str, fields: set[str]) -> dict[str, Any]:
    body = dict(value)
    identity = body.pop("identity", None)
    if set(body) != fields or identity != evidence_identity(namespace, body):
        raise ValueError(f"{namespace} evidence binding mismatch")
    return body


def replay_attempt(prepared: Mapping[str, Any], observation: Mapping[str, Any]) -> dict[str, Any]:
    """Replay retained evidence only; perform no filesystem or live I/O.

    A historical observation establishes what the host observed at that time.
    It is never a freshness or launch grant. Only the native host's exclusive
    claim transition can admit an actual protection attempt.
    """
    value = PreparedProtectionAttempt.model_validate_json(canonical_bytes(prepared))
    observed = _checked_evidence(
        observation,
        "runtime-protection-observation:v1",
        {
            "contract_version",
            "attempt_identity",
            "prepared_identity",
            "consumed",
            "baseline_matches",
            "state",
            "completion",
        },
    )
    if (
        observed["contract_version"] != "runtime-protection-observation-v1"
        or observed["attempt_identity"] != value.attempt.attempt_identity
        or observed["prepared_identity"] != value.identity
        or type(observed["consumed"]) is not bool
        or type(observed["baseline_matches"]) is not bool
    ):
        raise ValueError("observation belongs to another attempt or contract")
    state = "PREPARED" if not observed["consumed"] else "INCOMPLETE_RETAINED"
    completion = observed["completion"]
    if completion is not None:
        receipt = _checked_evidence(
            completion,
            "runtime-protection-completion:v1",
            {
                "contract_version",
                "attempt_identity",
                "prepared_identity",
                "packet_identity",
                "state",
                "child_launches",
                "exact_baseline_restored",
                "failure_count",
                "reusable",
            },
        )
        if (
            not observed["consumed"]
            or receipt["reusable"] is not False
            or receipt["contract_version"] != "runtime-protection-completion-v1"
            or receipt["attempt_identity"] != value.attempt.attempt_identity
            or receipt["prepared_identity"] != value.identity
            or type(receipt["exact_baseline_restored"]) is not bool
            or receipt["state"] not in ("COMPLETED_RESTORED", "INCOMPLETE_RETAINED")
            or receipt["exact_baseline_restored"] != (receipt["state"] == "COMPLETED_RESTORED")
        ):
            raise ValueError("completion provenance mismatch")
        state = receipt["state"]
    if observed["state"] != state:
        raise ValueError("invalid attempt lifecycle transition")
    return {
        "attempt_identity": value.attempt.attempt_identity,
        "state": state,
        "consumed": observed["consumed"],
        "baseline_matches": observed["baseline_matches"],
        "reusable": False,
        "execution_authorized": False,
    }


def validate_attempt_phase(
    prepared: Mapping[str, Any],
    packet: Mapping[str, Any],
    observation: Mapping[str, Any],
    *,
    phase: Literal["readiness", "packet"],
) -> dict[str, Any]:
    """Check offline readiness/packet bindings without treating history as authority."""
    if phase not in ("readiness", "packet"):
        raise ValueError("guard/Step-4 require the current native protection owner")
    value = PreparedProtectionAttempt.model_validate_json(canonical_bytes(prepared))
    if packet.get("operation") in ("guard_only", "guard_then_live"):
        from tradingagents.runtime_protection_handoff import build_guarded_launch_packet

        expected = build_guarded_launch_packet(
            prepared,
            guard_arguments=packet["child_arguments"],
            handoff=packet["handoff"],
            operation=packet["operation"],
        )
    else:
        expected = build_launch_packet(prepared, child_arguments=packet["child_arguments"])
    if dict(packet) != expected:
        raise ValueError("packet attempt/path/manifest/baseline binding mismatch")
    replay = replay_attempt(prepared, observation)
    if replay["consumed"]:
        raise ValueError("protection attempt already consumed")
    if not replay["baseline_matches"]:
        raise ValueError("prepared security/content baseline drift")
    return {
        "phase": phase,
        "attempt_identity": value.attempt.attempt_identity,
        "packet_identity": expected["identity"],
        "execution_authorized": False,
    }

"""Forward native materialization/handoff contracts with the complete caveat.

None of these builders authenticate a person, create native objects or authorize
Ticket-07/live execution. The registered native owner revalidates every use.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from pathlib import PureWindowsPath
from typing import Any, ClassVar, Literal

from pydantic import Field, model_validator
from typing_extensions import Self

from tradingagents.policy_qualified_contract import (
    Candidate,
    Closed,
    Digest,
    DownstreamEnvelope,
    EvidenceRef,
    Identity,
    Sealed,
)
from tradingagents.policy_qualified_results import PolicyQualifiedVerifier
from tradingagents.runtime_protection import (
    ConditionalProtectionAttempt,
    ContentManifest,
    PreparedProtectionAttempt,
    _materialization_payload,
    _windows_path,
    canonical_bytes,
    evidence_identity,
    validate_conditional_attempt,
)


class NativeOwnerBinding(Closed):
    contract_version: Literal["policy-qualified-native-owner-binding-v1"]
    instance: str = Field(pattern=r"^(?:operational|test)-[0-9]{8}-[0-9]{3}$")
    storage_binding_identity: Identity
    native_owner_sha256: Digest
    worker_manifest_sha256: Digest
    native_consumer_source_identity: Identity
    execution_authorized: Literal[False]


class NativePayloadProvenance(Sealed):
    namespace = "policy-qualified-native-payload-provenance:v1"
    contract_version: Literal["policy-qualified-native-payload-provenance-v1"]
    candidate: Candidate
    preparation_lineage_identity: Identity
    qualified_source: EvidenceRef
    qualified_runtime: EvidenceRef
    runtime_content: ContentManifest
    app_content: ContentManifest
    collection_provenance: EvidenceRef
    execution_authorized: Literal[False]
    identity: Identity

    @model_validator(mode="after")
    def _ancestry(self) -> Self:
        for component, role, ancestor in (
            (self.runtime_content, "runtime", self.qualified_runtime),
            (self.app_content, "app", self.qualified_source),
        ):
            if (
                component.role != role
                or component.source_commit != self.candidate.commit
                or component.source_tree != self.candidate.tree
                or component.ancestry_identity != ancestor.identity
            ):
                raise ValueError(
                    "native content is not bound to its exact qualified source/runtime ancestry"
                )
        return self


class InertFixtureLaunch(Sealed):
    namespace = "policy-owner-inert-fixture-launch:v1"
    contract_version: Literal["policy-owner-inert-fixture-launch-v1"]
    instance: str = Field(pattern=r"^test-[0-9]{8}-[0-9]{3}$")
    attempt_identity: Identity
    prepared_identity: Identity
    control_identity: Identity
    materialization_identity: Identity
    runtime_content_identity: Identity
    app_content_identity: Identity
    interpreter_sha256: Digest
    entrypoint_sha256: Digest
    child_arguments: tuple[str, ...]
    operation: Literal["inert", "guard_only"]
    maximum_child_seconds: int = Field(gt=0, le=120)
    reviewed_fixture_source: tuple[EvidenceRef, ...] = Field(min_length=1)
    label: Literal["ISOLATED_NATIVE_INERT_FIXTURE_ONLY"]
    ticket07_or_live_authorized: Literal[False]
    qualification_execution_authorized: Literal[False]
    identity: Identity


class PreparedConditionalProtectionAttempt(PreparedProtectionAttempt):
    identity_namespace: ClassVar[str] = "policy-qualified-runtime-protection-prepared:v2"
    contract_version: Literal["policy-qualified-runtime-protection-prepared-v2"]
    attempt: ConditionalProtectionAttempt
    owner_binding: NativeOwnerBinding


def seal_conditional_materialization_plan(
    *,
    attempt: object,
    owner_binding: object,
    policy_verifier: PolicyQualifiedVerifier,
    runtime_content: Mapping[str, Any],
    app_content: Mapping[str, Any],
    source_locations: Mapping[str, str],
    deployment_configuration: Mapping[str, Any],
    environment: Mapping[str, str],
    working_directory: str,
    python_version: str,
    credential_bindings: Sequence[Mapping[str, str]] = (),
) -> dict[str, Any]:
    verified = validate_conditional_attempt(attempt, policy_verifier=policy_verifier)
    owner = NativeOwnerBinding.read(owner_binding)
    payload = {
        "contract_version": "policy-qualified-runtime-protection-materialization-v2",
        "owner_binding": owner.model_dump(mode="json"),
        **_materialization_payload(
            attempt=verified,
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
        "identity": evidence_identity(
            "policy-qualified-runtime-protection-materialization:v2", payload
        ),
    }


def build_conditional_inert_control(
    prepared: object, *, child_arguments: Sequence[str], policy_verifier: PolicyQualifiedVerifier
) -> dict[str, Any]:
    value = PreparedConditionalProtectionAttempt.model_validate_json(
        canonical_bytes(
            prepared.model_dump(mode="json")
            if isinstance(prepared, PreparedConditionalProtectionAttempt)
            else prepared
        )
    )
    policy_verifier.verify_downstream(value.attempt.inputs.acceptance)
    arguments = list(child_arguments)
    if (
        len(arguments) < 3
        or arguments[:2] != ["-I", "-B"]
        or not all(isinstance(argument, str) and "\0" not in argument for argument in arguments)
    ):
        raise ValueError("conditional inert startup requires sealed interpreter arguments")
    entry = PureWindowsPath(_windows_path(arguments[2]))
    if not entry.is_relative_to(PureWindowsPath(value.attempt.paths.private_app_root)):
        raise ValueError("conditional inert entrypoint is outside the protected application")
    payload = {
        "contract_version": "policy-qualified-runtime-protection-control-v2",
        "operation": "inert",
        "prepared_identity": value.identity,
        "attempt_identity": value.attempt.attempt_identity,
        "paths": value.attempt.paths.model_dump(mode="json"),
        "materialization_identity": value.materialization_identity,
        "runtime_manifest_identity": value.runtime_manifest_identity,
        "app_manifest_identity": value.app_manifest_identity,
        "baseline_identity": value.baseline_identity,
        "protection_contract_version": value.protection_contract_version,
        "owner_binding": value.owner_binding.model_dump(mode="json"),
        "acceptance": value.attempt.inputs.acceptance.model_dump(mode="json"),
        "child_arguments": arguments,
        "execution_authorized": False,
    }
    return {
        **payload,
        "identity": evidence_identity("policy-qualified-runtime-protection-control:v2", payload),
    }


class ConditionalNativeCompletion(Closed):
    contract_version: Literal["policy-qualified-runtime-protection-completion-v2"]
    attempt_identity: Identity
    prepared_identity: Identity
    packet_identity: Identity
    owner_binding: NativeOwnerBinding
    acceptance: DownstreamEnvelope
    state: Literal["COMPLETED_RESTORED", "INCOMPLETE_RETAINED"]
    child_launches: int = Field(ge=0)
    exact_baseline_restored: bool
    failure_count: int = Field(ge=0)
    reusable: Literal[False]
    execution_authorized: Literal[False]
    identity: Identity

    @model_validator(mode="after")
    def _seal(self) -> Self:
        if self.exact_baseline_restored != (
            self.state == "COMPLETED_RESTORED"
        ) or self.identity != evidence_identity(
            "policy-qualified-runtime-protection-completion:v2",
            self.model_dump(mode="json", exclude={"identity"}),
        ):
            raise ValueError("conditional native completion lost its complete binding")
        return self

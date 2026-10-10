"""Versioned compact evidence descriptors, separate from staged bytes and consent.

Transfer bounds are implementation limits, not Windows limits. Whole documents
are still parsed by consumers; streaming transport does not imply constant-memory
JSON validation. Only the registered owner can authenticate these descriptors.
"""

from __future__ import annotations

import hashlib
import os
import re
import stat
from datetime import timedelta
from pathlib import Path
from typing import Literal

from pydantic import Field, model_validator
from typing_extensions import Self

from tradingagents.policy_owner_contract import Instance
from tradingagents.policy_qualified_contract import (
    Candidate,
    EvidenceRef,
    Identity,
    Sealed,
    Text,
    content_identity,
    strict_json,
    utc,
)

CHUNK_BYTES = 1024 * 1024
MAX_ARTIFACT_BYTES = 256 * 1024 * 1024
MAX_DESCRIPTOR_BYTES = 16 * 1024
MAX_IMPORT_SECONDS = 3600
MAX_PENDING_IMPORTS = 8
MAX_STORE_BYTES = 8 * 1024 * 1024 * 1024
MAX_TRANSFER_RECORDS = 4096


class ArtifactDescriptor(Sealed):
    """Exact human-review body; its digest is not an authentication credential."""

    namespace = "policy-owner-artifact-descriptor:v2"
    contract_version: Literal["policy-owner-artifact-descriptor-v2"]
    operation: Literal["REGISTER_EVIDENCE"]
    instance: Instance
    role_identity: Identity
    approval_identity: Identity
    candidate: Candidate
    lineage_plan_identity: Identity
    acquisition_identity: Text
    stage: Literal[
        "CURRENT_SOURCE", "ISO02", "ISO03", "QCF17", "ISO04_TICKET06", "REVIEWS", "CLOSURE_HANDOFF"
    ]
    phase: Literal["before", "after", "evidence"]
    artifact_schema: str = Field(pattern=r"^[a-z][a-z0-9-]*:v[1-9][0-9]*$")
    artifact_identity_field: str = Field(pattern=r"^[a-z][a-z0-9_]{0,63}$")
    artifact: EvidenceRef
    issued_utc: Text
    expires_utc: Text
    identity: Identity

    @model_validator(mode="after")
    def _bounds(self) -> Self:
        if not 0 < self.artifact.pin.byte_length <= MAX_ARTIFACT_BYTES:
            raise ValueError("artifact length exceeds the versioned transfer bound")
        if not self.artifact.identity.startswith(self.artifact_schema + ":sha256:"):
            raise ValueError("artifact schema and semantic identity disagree")
        if (
            not timedelta(0)
            < utc(self.expires_utc) - utc(self.issued_utc)
            <= timedelta(seconds=MAX_IMPORT_SECONDS)
        ):
            raise ValueError("descriptor validity exceeds the finite import lifetime")
        if len(self.to_bytes()) > MAX_DESCRIPTOR_BYTES:
            raise ValueError("compact descriptor exceeds its size bound")
        return self


def read_complete_artifact(path: Path, descriptor: ArtifactDescriptor) -> bytes:
    """Check the opened regular object and complete bytes, never just its name."""
    descriptor = ArtifactDescriptor.read(descriptor)
    before = path.lstat()
    if (
        not stat.S_ISREG(before.st_mode)
        or before.st_nlink != 1
        or getattr(before, "st_file_attributes", 0) & 0x400
    ):
        raise ValueError("artifact must be a regular unaliased file")
    if before.st_size != descriptor.artifact.pin.byte_length:
        raise ValueError("artifact length pin mismatch")
    digest = hashlib.sha256()
    chunks = []
    with path.open("rb") as stream:
        opened = os.fstat(stream.fileno())
        if (opened.st_dev, opened.st_ino) != (before.st_dev, before.st_ino):
            raise ValueError("artifact opened-object identity changed")
        count = 0
        while chunk := stream.read(CHUNK_BYTES):
            count += len(chunk)
            if count > descriptor.artifact.pin.byte_length:
                raise ValueError("artifact length pin changed during read")
            digest.update(chunk)
            chunks.append(chunk)
        after = os.fstat(stream.fileno())
    if (opened.st_size, opened.st_mtime_ns, opened.st_ctime_ns) != (
        after.st_size,
        after.st_mtime_ns,
        after.st_ctime_ns,
    ):
        raise ValueError("artifact changed during read")
    if (
        count != descriptor.artifact.pin.byte_length
        or digest.hexdigest() != descriptor.artifact.pin.sha256
    ):
        raise ValueError("artifact whole-file digest pin mismatch")
    return b"".join(chunks)


def validate_artifact(path: Path, descriptor: ArtifactDescriptor, *, coverage_context=None) -> dict:
    """Validate the full logical JSON and raw root identities, without admission."""
    descriptor = ArtifactDescriptor.read(descriptor)
    raw = read_complete_artifact(path, descriptor)
    if descriptor.artifact_schema == "policy-evidence-bytes:v1":
        if (
            descriptor.artifact_identity_field != "byte_digest"
            or descriptor.artifact.identity
            != "policy-evidence-bytes:v1:sha256:" + descriptor.artifact.pin.sha256
        ):
            raise ValueError("opaque evidence schema/digest identity mismatch")
        return {"opaque_complete_bytes": True}
    value = strict_json(raw)
    if (
        not isinstance(value, dict)
        or value.get(descriptor.artifact_identity_field) != descriptor.artifact.identity
    ):
        raise ValueError("artifact semantic identity field/schema mismatch")
    body = {key: item for key, item in value.items() if key != descriptor.artifact_identity_field}
    if content_identity(descriptor.artifact_schema, body) != descriptor.artifact.identity:
        raise ValueError("artifact canonical semantic identity mismatch")
    if "candidate" in value and Candidate.read(value["candidate"]) != descriptor.candidate:
        raise ValueError("artifact candidate differs from exact descriptor")
    binding = value.get("binding")
    if isinstance(binding, dict) and (
        binding.get("approval_identity") != descriptor.approval_identity
        or binding.get("lineage_plan_identity") != descriptor.lineage_plan_identity
    ):
        raise ValueError("artifact acceptance binding differs from descriptor approval/plan")
    if descriptor.artifact_schema == "policy-coverage-inventory:v2":
        from tradingagents.policy_coverage_contract import validate_current_inventory

        if coverage_context is None:
            raise ValueError("coverage inventory requires exact scope and measurement context")
        validate_current_inventory(value, *coverage_context)
        if (
            value["phase"] != descriptor.phase
            or value["instance"] != descriptor.instance
            or value["capture"]["observation_identity"] != descriptor.acquisition_identity
        ):
            raise ValueError("coverage acquisition differs from its exact descriptor")
        return value
    if descriptor.artifact_schema == "crash-protection-historical-inventory:v1":
        from tradingagents.policy_qualified_results import PolicyQualifiedVerifier

        PolicyQualifiedVerifier._roots(value)
        if (
            value.get("no_acl_write_or_recovery") is not True
            or type(value.get("file_count")) is not int
            or type(value.get("unreadable_count")) is not int
            or type(value.get("elapsed_seconds")) not in {int, float}
            or value["elapsed_seconds"] < 0
            or value["file_count"] != sum(root.get("file_count", -1) for root in value["roots"])
            or value["unreadable_count"] != 0
        ):
            raise ValueError(
                "raw inventory summary/coverage schema mismatch or incomplete evidence"
            )
        if value.get("phase") != descriptor.phase:
            raise ValueError("raw acquisition phase differs from descriptor")
        for root in value["roots"]:
            if set(root) != {
                "path",
                "entries",
                "file_count",
                "unreadable_count",
                "security_descriptors_requested",
                "inventory_identity",
            }:
                raise ValueError("raw inventory root schema mismatch")
            if (
                type(root["security_descriptors_requested"]) is not bool
                or root["unreadable_count"] != 0
            ):
                raise ValueError("raw inventory is incomplete or has invalid coverage")
            if root["file_count"] != sum(row["kind"] == "file" for row in root["entries"]) or root[
                "unreadable_count"
            ] != sum(row["kind"].startswith("unreadable") for row in root["entries"]):
                raise ValueError("raw inventory coverage counts disagree")
            for row in root["entries"]:
                if row.get("kind") not in {"file", "directory"} or not isinstance(
                    row.get("path"), str
                ):
                    raise ValueError(
                        "unreadable/reparse/missing/unsupported raw row is not admissible"
                    )
                if root["security_descriptors_requested"] and (
                    not {"attributes", "mtime_ns", "ctime_ns", "descriptor_sha256"} <= set(row)
                    or type(row["mtime_ns"]) is not int
                    or type(row["ctime_ns"]) is not int
                    or not re.fullmatch(r"[0-9a-f]{64}", str(row["descriptor_sha256"]))
                ):
                    raise ValueError("requested raw security/attribute coverage is incomplete")
                if "attributes" in row and (
                    type(row["attributes"]) is not int or not 0 <= row["attributes"] <= 0xFFFFFFFF
                ):
                    raise ValueError("raw attributes must be exact unsigned integers")
                if row["kind"] == "file" and (
                    type(row.get("byte_length")) is not int
                    or row["byte_length"] < 0
                    or not re.fullmatch(r"[0-9a-f]{64}", str(row.get("sha256")))
                ):
                    raise ValueError("raw file inventory schema mismatch")
    else:
        _validate_canonical_model(raw, descriptor)
    return value


def _validate_canonical_model(raw: bytes, descriptor: ArtifactDescriptor) -> None:
    """Use existing stage schemas; an arbitrary namespace is not a schema grant."""
    from tradingagents import policy_qualified_contract as contract
    from tradingagents.policy_evidence_acquisition import ProductionInventory
    from tradingagents.windows_sandbox_acceptance import (
        Ticket06AcceptanceResult,
        Ticket06CompletionMarker,
    )
    from tradingagents.windows_sandbox_qualification import (
        ISO03HostQualificationReport,
        ISO04Ticket06AcceptanceReport,
        ProtectedHostInventory,
    )
    from tradingagents.windows_sandbox_runner import (
        ReviewedSourceIdentity,
        RuntimeBundleInputContract,
    )

    sealed = [ProductionInventory]
    from tradingagents import policy_coverage_contract, policy_coverage_models

    for module in (policy_coverage_contract, policy_coverage_models):
        sealed.extend(
            model
            for model in vars(module).values()
            if isinstance(model, type) and issubclass(model, Sealed) and hasattr(model, "namespace")
        )
    sealed.extend(
        model
        for model in vars(contract).values()
        if isinstance(model, type) and issubclass(model, Sealed) and hasattr(model, "namespace")
    )
    for model in sealed:
        if model.namespace == descriptor.artifact_schema:
            if model.identity_field != descriptor.artifact_identity_field:
                raise ValueError("artifact schema identity field mismatch")
            model.read(raw)
            return
    for model, field in [
        (ReviewedSourceIdentity, "source_input_identity"),
        (RuntimeBundleInputContract, "runtime_bundle_contract_identity"),
        (ISO03HostQualificationReport, "report_identity"),
        (ISO04Ticket06AcceptanceReport, "report_identity"),
        (ProtectedHostInventory, "inventory_identity"),
        (Ticket06AcceptanceResult, "result_identity"),
        (Ticket06CompletionMarker, "marker_identity"),
    ]:
        pattern = model.model_json_schema()["properties"][field].get("pattern")
        if pattern and re.fullmatch(pattern, descriptor.artifact.identity):
            if descriptor.artifact_identity_field != field:
                raise ValueError("canonical artifact identity field mismatch")
            parsed = model.model_validate_json(raw)
            if isinstance(parsed, ReviewedSourceIdentity) and (
                parsed.commit_id != descriptor.candidate.commit
                or parsed.tree_hash != descriptor.candidate.tree
            ):
                raise ValueError("reviewed-source artifact differs from descriptor candidate")
            return
    raise ValueError("unsupported artifact schema; no implicit legacy downgrade")

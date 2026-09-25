"""Bounded chunk client; staging and complete retrieval never select consent."""

from __future__ import annotations

import base64
import hashlib
import time
from collections.abc import Callable
from pathlib import Path

from tradingagents.policy_evidence_contract import (
    CHUNK_BYTES,
    MAX_ARTIFACT_BYTES,
    MAX_IMPORT_SECONDS,
    ArtifactDescriptor,
    validate_artifact,
)
from tradingagents.policy_qualified_contract import EvidenceRef


def stage_artifact(
    call: Callable[[str, dict], dict], path: Path, descriptor: ArtifactDescriptor, *, progress=None
) -> dict:
    descriptor = ArtifactDescriptor.read(descriptor)
    validate_artifact(path, descriptor)
    started = time.monotonic()
    response = call("evidence-import-begin-v2", {"descriptor": descriptor.model_dump(mode="json")})
    transfer = response["transfer_id"]
    if (
        response.get("state") != "PENDING_UNAUTHENTICATED"
        or response.get("offset") != 0
        or response.get("staged_only") is not True
    ):
        raise ValueError("native import start did not retain its unauthenticated state")
    if progress:
        progress({"transfer_id": transfer, "state": "PENDING_UNAUTHENTICATED", "offset": 0})
    digest = hashlib.sha256()
    offset = 0
    with path.open("rb") as stream:
        while chunk := stream.read(CHUNK_BYTES):
            if time.monotonic() - started >= MAX_IMPORT_SECONDS:
                raise TimeoutError("bounded import expired; no authentication or automatic retry")
            if offset + len(chunk) > descriptor.artifact.pin.byte_length:
                raise ValueError("source length changed during transfer")
            digest.update(chunk)
            response = call(
                "evidence-import-chunk-v2",
                {
                    "transfer_id": transfer,
                    "offset": offset,
                    "bytes_base64": base64.b64encode(chunk).decode("ascii"),
                },
            )
            offset += len(chunk)
            if (
                response.get("offset") != offset
                or response.get("state") != "PENDING_UNAUTHENTICATED"
            ):
                raise ValueError("native import offset/state mismatch")
            if progress:
                progress(
                    {"transfer_id": transfer, "state": "PENDING_UNAUTHENTICATED", "offset": offset}
                )
    if (
        offset != descriptor.artifact.pin.byte_length
        or digest.hexdigest() != descriptor.artifact.pin.sha256
    ):
        raise ValueError("source bytes changed during import; pending bytes are not admitted")
    result = call("evidence-import-finish-v2", {"transfer_id": transfer})
    if (
        result.get("state") != "PUBLISHED_UNAUTHENTICATED"
        or result.get("offset") != offset
        or result.get("staged_only") is not True
    ):
        raise ValueError("native publication was not confirmed")
    return {
        "contract_version": "policy-owner-evidence-intake-v2",
        "kind": "artifact-reference",
        "key": descriptor.artifact.identity,
        "value": {"transfer_id": transfer, "descriptor": descriptor.model_dump(mode="json")},
    }


def export_artifact(
    call: Callable[[str, dict], dict], reference: EvidenceRef, destination: Path
) -> dict:
    """Publish only complete verified bytes; retain an interrupted partial file."""
    reference = EvidenceRef.read(reference)
    if not 0 < reference.pin.byte_length <= MAX_ARTIFACT_BYTES:
        raise ValueError("export exceeds the versioned artifact bound")
    from tradingagents.policy_evidence_acquisition import new_evidence_output

    digest = hashlib.sha256()
    started = time.monotonic()
    offset = 0
    with new_evidence_output(destination) as stream:
        while offset < reference.pin.byte_length:
            if time.monotonic() - started >= MAX_IMPORT_SECONDS:
                raise TimeoutError("bounded export expired; partial bytes retained")
            count = min(CHUNK_BYTES, reference.pin.byte_length - offset)
            result = call(
                "evidence-read-chunk-v2",
                {"reference": reference.model_dump(mode="json"), "offset": offset, "count": count},
            )
            if (
                result.get("contract_version") != "policy-owner-evidence-read-v2"
                or result.get("reference") != reference.model_dump(mode="json")
                or result.get("offset") != offset
            ):
                raise ValueError("registered export context mismatch")
            raw = base64.b64decode(result["bytes_base64"], validate=True)
            if len(raw) != count:
                raise ValueError("registered export short read")
            stream.write(raw)
            digest.update(raw)
            offset += len(raw)
        if digest.hexdigest() != reference.pin.sha256:
            raise ValueError("complete export digest mismatch; partial retained")
    return {
        "reference": reference.model_dump(mode="json"),
        "byte_length": offset,
        "elapsed_seconds": time.monotonic() - started,
        "execution_authorized": False,
    }

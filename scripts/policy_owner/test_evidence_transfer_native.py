"""Actual native chunk-store fixtures; no service, enrollment or human record."""

from __future__ import annotations

import argparse
import base64
import json
import os
import subprocess
import sys
import time
from datetime import datetime, timedelta, timezone
from hashlib import sha256
from pathlib import Path


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--native-fixture", required=True, type=Path)
    parser.add_argument("--dependencies", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    args = parser.parse_args()
    if not (sys.flags.isolated and sys.flags.no_site and sys.dont_write_bytecode):
        raise ValueError("fixture requires -I -B -S")
    source = Path(__file__).resolve().parents[2]
    output = args.output.resolve()
    if (
        output.exists()
        or output.is_relative_to(source)
        or output.is_relative_to(Path(os.environ["PROGRAMDATA"]))
    ):
        raise ValueError("fresh external disposable fixture output required")
    if args.native_fixture.name != "EvidenceTransferContracts.exe":
        raise ValueError("only the headless non-authenticating fixture is allowed")
    sys.path[:0] = [str(source), str(args.dependencies.resolve(strict=True))]
    from tradingagents.policy_evidence_contract import ArtifactDescriptor, validate_artifact
    from tradingagents.policy_qualified_contract import BytePin, canonical_bytes, content_identity

    output.mkdir()
    store = output / "disposable-store"
    for child in ("artifacts", "state/temp"):
        (store / child).mkdir(parents=True)
    (store / "UNAUTHENTICATED-TEST-STORE.txt").write_bytes(
        b"NO HUMAN AUTHENTICATION OR OPERATIONAL AUTHORITY\n"
    )
    instance = "test-20300101-001"
    calls = []

    def call(operation, payload, *, reject=None, selected_instance=instance):
        started = time.monotonic()
        request = canonical_bytes({"operation": operation, "payload": payload})
        completed = subprocess.run(
            [str(args.native_fixture), str(store), selected_instance],
            input=request,
            capture_output=True,
            timeout=35,
            creationflags=subprocess.CREATE_NO_WINDOW,
            cwd=output,
        )
        record = {
            "operation": operation,
            "request_bytes": len(request),
            "response_bytes": len(completed.stdout),
            "returncode": completed.returncode,
            "elapsed_seconds": time.monotonic() - started,
            "stderr": completed.stderr.decode("utf-8", "replace"),
            "expected_rejection": reject,
        }
        calls.append(record)
        (output / "calls.json").write_text(json.dumps(calls, indent=2), encoding="utf-8")
        if reject:
            assert completed.returncode != 0 and reject in record["stderr"], record
            return None
        assert completed.returncode == 0, record
        return json.loads(completed.stdout)

    rows = [{"path": ".", "kind": "directory", "attributes": 268435472}]
    rows.extend(
        {
            "path": f"fixture-{index:05}.txt",
            "kind": "file",
            "byte_length": index + 1,
            "sha256": sha256(str(index).encode()).hexdigest(),
        }
        for index in range(200)
    )
    root = {
        "path": str(output / "synthetic-inventory-root"),
        "entries": rows,
        "file_count": 200,
        "unreadable_count": 0,
        "security_descriptors_requested": False,
        "inventory_identity": content_identity(
            "crash-protection-historical-root-inventory:v1", rows
        ),
    }
    body = {
        "phase": "before",
        "roots": [root],
        "elapsed_seconds": 0.01,
        "file_count": 200,
        "unreadable_count": 0,
        "no_acl_write_or_recovery": True,
    }
    body["identity"] = content_identity("crash-protection-historical-inventory:v1", body)
    raw = canonical_bytes(body) + b"\n"
    now = datetime.now(timezone.utc)
    descriptor = ArtifactDescriptor.seal(
        contract_version="policy-owner-artifact-descriptor-v2",
        operation="REGISTER_EVIDENCE",
        instance=instance,
        role_identity="policy-owner-project-role-proposal:v1:sha256:" + "1" * 64,
        approval_identity="historical-state-operator-approval:v1:sha256:" + "2" * 64,
        candidate={"commit": "a" * 40, "tree": "b" * 40, "parent": "c" * 40},
        lineage_plan_identity="historical-state-lineage-plan:v1:sha256:" + "3" * 64,
        acquisition_identity="synthetic-native-fixture-only-001",
        stage="ISO02",
        phase="before",
        artifact_schema="crash-protection-historical-inventory:v1",
        artifact_identity_field="identity",
        artifact={"identity": body["identity"], "pin": BytePin.of(raw).model_dump(mode="json")},
        issued_utc=now.isoformat(),
        expires_utc=(now + timedelta(hours=1)).isoformat(),
    )
    started = call("evidence-import-begin-v2", {"descriptor": descriptor.model_dump(mode="json")})
    transfer = started["transfer_id"]
    assert started["state"] == "PENDING_UNAUTHENTICATED"
    call("evidence-import-finish-v2", {"transfer_id": transfer}, reject="length mismatch")
    call(
        "evidence-import-chunk-v2",
        {"transfer_id": transfer, "offset": 1, "bytes_base64": base64.b64encode(raw).decode()},
        reject="declared artifact",
    )
    call(
        "evidence-import-chunk-v2",
        {"transfer_id": transfer, "offset": 0, "bytes_base64": base64.b64encode(raw).decode()},
    )
    call(
        "evidence-import-chunk-v2",
        {"transfer_id": transfer, "offset": 0, "bytes_base64": "YQ=="},
        reject="replayed",
    )
    call(
        "evidence-import-inspect-v2",
        {"transfer_id": transfer},
        selected_instance="test-20300101-002",
        reject="instance mismatch",
    )
    published = call("evidence-import-finish-v2", {"transfer_id": transfer})
    assert published["state"] == "PUBLISHED_UNAUTHENTICATED" and published["staged_only"] is True
    call("evidence-import-finish-v2", {"transfer_id": transfer}, reject="replayed")
    retrieved = call(
        "evidence-read-chunk-v2",
        {"reference": descriptor.artifact.model_dump(mode="json"), "offset": 0, "count": len(raw)},
    )
    assert base64.b64decode(retrieved["bytes_base64"]) == raw
    checked = validate_artifact(
        store / "artifacts" / (descriptor.artifact.pin.sha256 + ".blob"), descriptor
    )
    assert checked["roots"][0]["entries"][0]["attributes"] == 268435472
    result = {
        "status": "PASS_NATIVE_TRANSFER_FIXTURE_ONLY",
        "calls": len(calls),
        "artifact_bytes": len(raw),
        "descriptor_bytes": len(descriptor.to_bytes()),
        "human_authentication": False,
        "registered_owner": False,
        "operational_authority": False,
        "qualification": False,
        "complete_readback_equal": True,
        "raw_attributes_unchanged": True,
    }
    (output / "result.json").write_text(json.dumps(result, indent=2), encoding="utf-8")
    print(json.dumps(result))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

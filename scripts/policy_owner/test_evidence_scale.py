"""Production-size native transfer/readback on explicit unregistered TEST stores.

Generated inventories are labelled schema fixtures, never physical observations.
The retained historical corpus is transported byte-for-byte and never registered
as a new observation. No service/Hello/owner authentication is called here.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import subprocess
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--native-fixture", type=Path, required=True)
    parser.add_argument("--dependencies", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--sizes", type=int, nargs="*", default=[80 * 1024**2, 128 * 1024**2])
    parser.add_argument("--retained-corpus", type=Path)
    args = parser.parse_args()
    source = Path(__file__).resolve().parents[2]
    output = args.output.resolve()
    if (
        output.exists()
        or output.is_relative_to(source)
        or output.is_relative_to(Path(os.environ["PROGRAMDATA"]))
    ):
        raise ValueError("fresh external TEST scale output required")
    if args.native_fixture.name != "EvidenceTransferContracts.exe":
        raise ValueError("explicit headless native fixture required")
    sys.path[:0] = [
        str(source),
        str(args.dependencies.resolve(strict=True)),
        str(Path(__file__).resolve().parent),
    ]
    from evidence_test_metrics import disk_metrics, memory_metrics

    from tradingagents.policy_evidence_client import export_artifact, stage_artifact
    from tradingagents.policy_evidence_contract import (
        MAX_ARTIFACT_BYTES,
        ArtifactDescriptor,
        validate_artifact,
    )
    from tradingagents.policy_evidence_limits import bound_evidence_process
    from tradingagents.policy_qualified_contract import BytePin, canonical_bytes, content_identity
    from tradingagents.policy_qualified_results import PolicyQualifiedVerifier

    bound_evidence_process(seconds=3660)
    output.mkdir()
    store = output / "UNREGISTERED-TEST-STORE"
    for child in ("artifacts", "state/temp"):
        (store / child).mkdir(parents=True)
    (store / "UNAUTHENTICATED-TEST-STORE.txt").write_bytes(
        b"NO HUMAN AUTHENTICATION OR OPERATIONAL AUTHORITY\n"
    )
    instance = "test-20300101-001"
    aggregate = {
        "native_calls": 0,
        "request_bytes": 0,
        "response_bytes": 0,
        "maximum_request_bytes": 0,
    }
    cases = []

    def call(operation, payload):
        raw = canonical_bytes({"operation": operation, "payload": payload})
        response = subprocess.run(
            [str(args.native_fixture), str(store), instance],
            input=raw,
            capture_output=True,
            timeout=35,
            creationflags=subprocess.CREATE_NO_WINDOW,
            cwd=output,
        )
        aggregate["native_calls"] += 1
        aggregate["request_bytes"] += len(raw)
        aggregate["response_bytes"] += len(response.stdout)
        aggregate["maximum_request_bytes"] = max(aggregate["maximum_request_bytes"], len(raw))
        if response.returncode:
            raise ValueError(response.stderr.decode("utf-8", "replace"))
        return json.loads(response.stdout)

    def make_fixture(target, ordinal):
        if not 1024 * 1024 <= target <= MAX_ARTIFACT_BYTES:
            raise ValueError("fixture size outside supported artifact range")
        first = {
            "path": ".",
            "kind": "directory",
            "attributes": 16,
            "mtime_ns": 101,
            "ctime_ns": 100,
            "descriptor_sha256": "f" * 64,
        }

        def row(index):
            return {
                "path": f"fixture-{index:08x}.dat",
                "kind": "file",
                "byte_length": 1024,
                "sha256": hashlib.sha256(
                    f"{ordinal}:{index}:complete-data-vector".encode()
                ).hexdigest(),
                "attributes": 32,
                "mtime_ns": 1000000000000000000 + index,
                "ctime_ns": 1000000000000000000,
                "descriptor_sha256": "e" * 64,
            }

        row_bytes = len(canonical_bytes(row(0))) + 1
        count = (target - 8192) // row_bytes
        rows = [first, *(row(index) for index in range(count))]
        root = {
            "path": "D:\\SYNTHETIC-SCHEMA-FIXTURE-NOT-OBSERVED",
            "entries": rows,
            "file_count": count,
            "unreadable_count": 0,
            "security_descriptors_requested": True,
            "inventory_identity": content_identity(
                "crash-protection-historical-root-inventory:v1", rows
            ),
        }
        body = {
            "phase": "before",
            "roots": [root],
            "elapsed_seconds": 0.0,
            "file_count": count,
            "unreadable_count": 0,
            "no_acl_write_or_recovery": True,
            "fixture_kind": "SYNTHETIC_SCHEMA_VECTOR_NOT_AN_ACQUISITION",
            "fixture_note": "",
        }

        def seal():
            body.pop("identity", None)
            body["identity"] = content_identity("crash-protection-historical-inventory:v1", body)
            return canonical_bytes(body) + b"\n"

        raw = seal()
        assert len(raw) <= target
        body["fixture_note"] = "x" * (target - len(raw))
        raw = seal()
        assert len(raw) == target
        path = output / f"fixture-{ordinal}.json"
        path.write_bytes(raw)
        return path, body["identity"], "before", len(rows)

    inputs = []
    for index, size in enumerate(args.sizes):
        started = time.monotonic()
        path, identity, phase, rows = make_fixture(size, index)
        inputs.append(
            (path, identity, phase, rows, "GENERATED_SCHEMA_FIXTURE", time.monotonic() - started)
        )
    if args.retained_corpus:
        raw = args.retained_corpus.read_bytes()
        saved = json.loads(raw)
        inputs.append(
            (
                args.retained_corpus,
                saved["identity"],
                saved["phase"],
                sum(len(root["entries"]) for root in saved["roots"]),
                "RETAINED_CORPUS_NOT_NEW_OBSERVATION",
                0,
            )
        )
        del saved, raw
    for ordinal, (path, identity, phase, rows, purpose, generated_seconds) in enumerate(inputs):
        raw = path.read_bytes()
        pin = BytePin.of(raw)
        del raw
        now = datetime.now(timezone.utc)
        retained = purpose == "RETAINED_CORPUS_NOT_NEW_OBSERVATION"
        descriptor = ArtifactDescriptor.seal(
            contract_version="policy-owner-artifact-descriptor-v2",
            operation="REGISTER_EVIDENCE",
            instance=instance,
            role_identity="policy-owner-project-role-proposal:v1:sha256:" + "1" * 64,
            approval_identity="historical-state-operator-approval:v1:sha256:" + "2" * 64,
            candidate={"commit": "a" * 40, "tree": "b" * 40, "parent": "c" * 40},
            lineage_plan_identity="historical-state-lineage-plan:v1:sha256:" + "3" * 64,
            acquisition_identity=purpose + "-" + str(ordinal),
            stage="ISO02",
            phase=phase,
            artifact_schema="policy-evidence-bytes:v1"
            if retained
            else "crash-protection-historical-inventory:v1",
            artifact_identity_field="byte_digest" if retained else "identity",
            artifact={
                "identity": "policy-evidence-bytes:v1:sha256:" + pin.sha256
                if retained
                else identity,
                "pin": pin.model_dump(mode="json"),
            },
            issued_utc=now.isoformat(),
            expires_utc=(now + timedelta(hours=1)).isoformat(),
        )
        started = time.monotonic()
        intake = stage_artifact(call, path, descriptor)
        import_seconds = time.monotonic() - started
        started = time.monotonic()
        verified = call("evidence-verify-complete-v2", intake["value"])
        assert verified["complete_bytes_verified"] is True and verified["authenticated"] is False
        verification_seconds = time.monotonic() - started
        export_path = output / f"readback-{ordinal}.json"
        result = export_artifact(call, descriptor.artifact, export_path)
        started = time.monotonic()
        original = validate_artifact(path, descriptor)
        retrieved = validate_artifact(export_path, descriptor)
        if retained:
            # Frozen original includes historical unreadable rows. Transport it
            # as opaque complete bytes, never as an admissible fresh acquisition.
            original = json.loads(path.read_bytes())
            retrieved = json.loads(export_path.read_bytes())
            assert original["identity"] == retrieved["identity"] == identity
            assert (
                content_identity(
                    "crash-protection-historical-inventory:v1",
                    {key: value for key, value in original.items() if key != "identity"},
                )
                == identity
            )
        assert PolicyQualifiedVerifier._roots(original) == PolicyQualifiedVerifier._roots(retrieved)
        compare_seconds = time.monotonic() - started
        unreadable = (
            sum(root["unreadable_count"] for root in original["roots"]) if retained else None
        )
        del original, retrieved
        document = canonical_bytes(intake)
        (output / f"UNAUTHENTICATED-intake-{ordinal}.json").write_bytes(document)
        case = {
            "purpose": purpose,
            "artifact_bytes": pin.byte_length,
            "sha256": pin.sha256,
            "root_entries": rows,
            "generation_seconds": generated_seconds,
            "import_seconds": import_seconds,
            "native_complete_verification_seconds": verification_seconds,
            "export_seconds": result["elapsed_seconds"],
            "full_schema_and_comparison_seconds": compare_seconds,
            "descriptor_bytes": len(descriptor.to_bytes()),
            "intake_bytes": len(document),
            "complete_readback_equal": True,
            "authenticated": False,
            "new_observation_registered": False,
            "qualification": False,
            "original_semantic_identity_preserved": identity,
            "transport_schema": descriptor.artifact_schema,
            "historical_unreadable_rows": unreadable,
            "memory": memory_metrics(),
        }
        cases.append(case)
        (output / "cases.json").write_text(json.dumps(cases, indent=2), encoding="utf-8")
        print(json.dumps(case), flush=True)
    disk = sum(path.stat().st_size for path in output.rglob("*") if path.is_file())
    report = {
        "status": "PASS_LARGE_NATIVE_TRANSFER_COMPARISON_ONLY",
        "cases": cases,
        "transport_totals": aggregate,
        "logical_file_bytes": disk,
        "disk": disk_metrics(output),
        "memory": memory_metrics(),
        "inline_payload_journal_bytes": 0,
        "compact_intake_bytes_total": sum(case["intake_bytes"] for case in cases),
        "authentication_or_operational_owner_called": False,
        "full_parser_materializes_json": True,
        "process_memory_hard_limit_bytes": 4 * 1024**3,
        "qualification_result": None,
    }
    (output / "result.json").write_text(json.dumps(report, indent=2), encoding="utf-8")
    print(
        json.dumps(
            {
                "status": report["status"],
                "cases": len(cases),
                "disk": report["disk"],
                "memory": report["memory"],
            }
        ),
        flush=True,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

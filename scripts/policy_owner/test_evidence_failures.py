"""Native rejection/atomicity/capacity matrix on disposable TEST objects only."""

from __future__ import annotations

import argparse
import base64
import concurrent.futures
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
    args = parser.parse_args()
    source = Path(__file__).resolve().parents[2]
    output = args.output.resolve()
    if (
        output.exists()
        or output.is_relative_to(source)
        or output.is_relative_to(Path(os.environ["PROGRAMDATA"]))
    ):
        raise ValueError("fresh external fixture root required")
    if args.native_fixture.name != "EvidenceTransferContracts.exe":
        raise ValueError("headless fixture binary required")
    sys.path[:0] = [
        str(source),
        str(args.dependencies.resolve()),
        str(Path(__file__).resolve().parent),
    ]
    from evidence_test_metrics import disk_metrics, memory_metrics

    from tradingagents.policy_evidence_contract import MAX_ARTIFACT_BYTES, ArtifactDescriptor
    from tradingagents.policy_evidence_limits import bound_evidence_process
    from tradingagents.policy_qualified_contract import BytePin, canonical_bytes, content_identity

    bound_evidence_process(seconds=1200)
    output.mkdir()
    results = []
    instance = "test-20300101-001"

    def store(name):
        root = output / name
        (root / "state/temp").mkdir(parents=True)
        (root / "artifacts").mkdir()
        (root / "UNAUTHENTICATED-TEST-STORE.txt").write_bytes(
            b"NO HUMAN AUTHENTICATION OR OPERATIONAL AUTHORITY\n"
        )
        return root

    def request(root, operation, payload, selected=instance):
        started = time.monotonic()
        result = subprocess.run(
            [str(args.native_fixture), str(root), selected],
            input=canonical_bytes({"operation": operation, "payload": payload}),
            capture_output=True,
            timeout=60,
            creationflags=subprocess.CREATE_NO_WINDOW,
            cwd=output,
        )
        return {
            "returncode": result.returncode,
            "stdout": result.stdout.decode("utf-8", "replace"),
            "stderr": result.stderr.decode("utf-8", "replace"),
            "elapsed_seconds": time.monotonic() - started,
        }

    def check(name, root, operation, payload, *, rejection=None, selected=instance):
        result = request(root, operation, payload, selected)
        if rejection:
            assert result["returncode"] != 0 and rejection in result["stderr"], (name, result)
        else:
            assert result["returncode"] == 0, (name, result)
        results.append({"case": name, "expected_rejection": rejection, **result})
        (output / "cases.json").write_text(json.dumps(results, indent=2), encoding="utf-8")
        return json.loads(result["stdout"]) if result["returncode"] == 0 else None

    raw = b"complete nonzero TEST bytes\x00\xff\x01"
    now = datetime.now(timezone.utc)
    descriptor = ArtifactDescriptor.seal(
        contract_version="policy-owner-artifact-descriptor-v2",
        operation="REGISTER_EVIDENCE",
        instance=instance,
        role_identity="policy-owner-project-role-proposal:v1:sha256:" + "1" * 64,
        approval_identity="historical-state-operator-approval:v1:sha256:" + "2" * 64,
        candidate={"commit": "a" * 40, "tree": "b" * 40, "parent": "c" * 40},
        lineage_plan_identity="historical-state-lineage-plan:v1:sha256:" + "3" * 64,
        acquisition_identity="SYNTHETIC_FAILURE_MATRIX_ONLY",
        stage="ISO02",
        phase="evidence",
        artifact_schema="policy-evidence-bytes:v1",
        artifact_identity_field="byte_digest",
        artifact={
            "identity": "policy-evidence-bytes:v1:sha256:" + BytePin.of(raw).sha256,
            "pin": BytePin.of(raw),
        },
        issued_utc=now.isoformat(),
        expires_utc=(now + timedelta(hours=1)).isoformat(),
    )
    document = descriptor.model_dump(mode="json")

    def altered(**changes):
        value = {key: item for key, item in document.items() if key != "identity"}
        value.update(changes)
        value["identity"] = content_identity("policy-owner-artifact-descriptor:v2", value)
        return value

    def begin(root, doc=document):
        response = request(root, "evidence-import-begin-v2", {"descriptor": doc})
        assert response["returncode"] == 0, response
        return json.loads(response["stdout"])["transfer_id"]

    root = store("bounds")
    check(
        "wrong-instance",
        root,
        "evidence-import-begin-v2",
        {"descriptor": document},
        rejection="instance mismatch",
        selected="test-20300101-002",
    )
    check(
        "arbitrary-file-path-not-an-operation",
        root,
        "evidence-import-begin-v2",
        {"descriptor": document, "source_path": "C:\\must-not-be-opened"},
        rejection="field",
    )
    check(
        "unsupported-downgrade",
        root,
        "evidence-import-begin-v2",
        {"descriptor": altered(contract_version="policy-owner-artifact-descriptor-v1")},
        rejection="version",
    )
    check(
        "expired-descriptor",
        root,
        "evidence-import-begin-v2",
        {
            "descriptor": altered(
                issued_utc=(now - timedelta(hours=2)).isoformat(),
                expires_utc=(now - timedelta(hours=1)).isoformat(),
            )
        },
        rejection="Expired",
    )
    check(
        "future-descriptor",
        root,
        "evidence-import-begin-v2",
        {
            "descriptor": altered(
                issued_utc=(now + timedelta(minutes=1)).isoformat(),
                expires_utc=(now + timedelta(minutes=2)).isoformat(),
            )
        },
        rejection="future",
    )
    check(
        "artifact-one-byte-over-limit",
        root,
        "evidence-import-begin-v2",
        {
            "descriptor": altered(
                artifact={
                    "identity": document["artifact"]["identity"],
                    "pin": {"byte_length": MAX_ARTIFACT_BYTES + 1, "sha256": "a" * 64},
                }
            )
        },
        rejection="byte bound",
    )
    check(
        "zero-length",
        root,
        "evidence-import-begin-v2",
        {
            "descriptor": altered(
                artifact={
                    "identity": document["artifact"]["identity"],
                    "pin": {"byte_length": 0, "sha256": "a" * 64},
                }
            )
        },
        rejection="byte bound",
    )
    transfer = begin(root)
    check(
        "path-escape-transfer-id",
        root,
        "evidence-import-finish-v2",
        {"transfer_id": "../outside"},
        rejection="identifier",
    )
    check(
        "short-read-before-publish",
        root,
        "evidence-import-finish-v2",
        {"transfer_id": transfer},
        rejection="length mismatch",
    )
    check("cancel-partial", root, "evidence-import-cancel-v2", {"transfer_id": transfer})
    check(
        "cancelled-never-admitted",
        root,
        "evidence-verify-complete-v2",
        {"transfer_id": transfer, "descriptor": document},
        rejection="cancelled",
    )
    transfer = begin(root)
    check(
        "wrong-digest-upload",
        root,
        "evidence-import-chunk-v2",
        {
            "transfer_id": transfer,
            "offset": 0,
            "bytes_base64": base64.b64encode(b"X" * len(raw)).decode(),
        },
    )
    check(
        "wrong-digest-rejects-publication",
        root,
        "evidence-import-finish-v2",
        {"transfer_id": transfer},
        rejection="digest mismatch",
    )
    check("cancel-corrupt-import", root, "evidence-import-cancel-v2", {"transfer_id": transfer})

    root = store("concurrent")
    transfer = begin(root)
    payload = {"transfer_id": transfer, "offset": 0, "bytes_base64": base64.b64encode(raw).decode()}
    with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
        races = list(
            pool.map(lambda _: request(root, "evidence-import-chunk-v2", payload), range(2))
        )
    assert sum(result["returncode"] == 0 for result in races) == 1, races
    results.append({"case": "concurrent-same-offset-one-winner", "results": races})
    check(
        "concurrent-winner-completes", root, "evidence-import-finish-v2", {"transfer_id": transfer}
    )
    blob = root / "artifacts" / (descriptor.artifact.pin.sha256 + ".blob")
    blob.write_bytes(b"X" * len(raw))  # attack on disposable fixture only; no installed store
    check(
        "changed-published-object",
        root,
        "evidence-verify-complete-v2",
        {"transfer_id": transfer, "descriptor": document},
        rejection="digest mismatch",
    )

    root = store("publication-crash")
    transfer = begin(root)
    check(
        "upload-before-publication-crash",
        root,
        "evidence-import-chunk-v2",
        {"transfer_id": transfer, "offset": 0, "bytes_base64": base64.b64encode(raw).decode()},
    )
    # Retain exactly the filesystem boundary after byte publication but before
    # completion marker. No accepted record exists, and verification must fail.
    part = root / "state/temp" / ("evidence-import-" + transfer + ".part")
    part.rename(root / "artifacts" / (descriptor.artifact.pin.sha256 + ".blob"))
    check(
        "crash-after-blob-before-marker-is-not-complete",
        root,
        "evidence-verify-complete-v2",
        {"transfer_id": transfer, "descriptor": document},
        rejection="Cannot open",
    )

    root = store("hardlink-attack")
    transfer = begin(root)
    part = root / "state/temp" / ("evidence-import-" + transfer + ".part")
    alias = root / "ATTACK-ALIAS"
    os.link(part, alias)
    check(
        "hardlink-part-cannot-be-written",
        root,
        "evidence-import-chunk-v2",
        {"transfer_id": transfer, "offset": 0, "bytes_base64": base64.b64encode(raw).decode()},
        rejection="Hard-linked",
    )
    assert alias.read_bytes() == b""

    root = store("pending-quota")
    for _ in range(8):
        begin(root)
    check(
        "ninth-pending-import-rejected",
        root,
        "evidence-import-begin-v2",
        {"descriptor": document},
        rejection="Pending import count",
    )

    root = store("store-quota")
    # Logical reservation is exercised without filling the host volume.
    with (root / "artifacts" / ("a" * 64 + ".blob")).open("xb") as stream:
        stream.truncate(8 * 1024**3)
    check(
        "declared-store-quota-rejected",
        root,
        "evidence-import-begin-v2",
        {"descriptor": document},
        rejection="disk quota",
    )

    root = store("journal-frame-bounds")
    for size in (32 * 1024**2 - 1, 32 * 1024**2, 32 * 1024**2 + 1):
        check(
            "journal-" + str(size),
            root,
            "fixture-journal-boundary",
            {"bytes": size},
            rejection="journal capacity" if size > 32 * 1024**2 else None,
        )
    for size in (64 * 1024**2 - 1, 64 * 1024**2, 64 * 1024**2 + 1):
        check(
            "frame-" + str(size),
            root,
            "fixture-frame-boundary",
            {"bytes": size},
            rejection="size exceeds" if size > 64 * 1024**2 else None,
        )
    docs = [
        {
            "contract_version": "policy-owner-evidence-intake-v2",
            "kind": "artifact-reference",
            "key": "fixture-" + str(i),
            "value": {"transfer_id": f"{i:032x}", "descriptor": document},
        }
        for i in range(64)
    ]
    check(
        "representative-cumulative-64-descriptor-journal",
        root,
        "fixture-journal-capacity",
        {"documents": docs},
    )
    report = {
        "status": "PASS_NATIVE_FAILURE_CAPACITY_FIXTURES_ONLY",
        "cases": results,
        "memory": memory_metrics(),
        "disk": disk_metrics(output),
        "human_authentication": False,
        "installed_owner_called": False,
        "qualification": False,
        "limitations": [
            "publication boundary crash is an explicit interrupted-store fixture, not a forced machine crash",
            "quota test uses logical file length; no host volume filled",
        ],
    }
    (output / "result.json").write_text(json.dumps(report, indent=2), encoding="utf-8")
    print(
        json.dumps({"status": report["status"], "cases": len(results), "memory": report["memory"]})
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

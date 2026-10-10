"""Generated large schema vectors through real native transfer and all validators.

No generated row is an actual native observation. Small native captures are a
separate test. No installed owner, genuine authentication or ISO execution.
"""

from __future__ import annotations

import argparse
import base64
import copy
import gc
import hashlib
import json
import os
import subprocess
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--output", type=Path, required=True)
    p.add_argument("--dependencies", type=Path, required=True)
    p.add_argument("--native-fixture", type=Path, required=True)
    p.add_argument("--file-count", type=int, default=200000)
    args = p.parse_args()
    source = Path(__file__).resolve().parents[2]
    out = args.output.absolute()
    if (
        out.exists()
        or out.is_relative_to(source)
        or out.is_relative_to(Path(os.environ["PROGRAMDATA"]))
    ):
        raise ValueError("fresh disposable scale output required")
    if (
        not 1 <= args.file_count <= 300000
        or args.native_fixture.name != "EvidenceTransferContracts.exe"
    ):
        raise ValueError("bounded explicit native fixture required")
    out.mkdir()
    sys.path[:0] = [str(source), str(args.dependencies)]
    # Bind proof to bytes actually imported, rather than a later source snapshot.
    tested_files = [
        *source.glob("tradingagents/policy_*.py"),
        source / "tradingagents/runtime_protection.py",
        source / "tradingagents/qualified_routing_composition.py",
        source / "scripts/policy_owner/predeployment_fixtures.py",
        source / "scripts/policy_owner/registered_test_fixtures.py",
        source / "scripts/policy_owner/test_coverage_scale.py",
        *source.glob("tests/fixtures/*coverage*.py"),
    ]

    def source_pins():
        return {
            path.relative_to(source).as_posix(): {
                "byte_length": path.stat().st_size,
                "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
            }
            for path in sorted(set(tested_files))
        }

    initial_pins = source_pins()
    native_paths = [
        args.native_fixture,
        args.native_fixture.with_name("CoverageEvidenceContracts.exe"),
        args.native_fixture.with_name("build.json"),
    ]

    def native_pins():
        return {
            path.name: {
                "path": str(path),
                "byte_length": path.stat().st_size,
                "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
            }
            for path in native_paths
        }

    initial_native_pins = native_pins()
    (out / "SOURCE-PINS-START.json").write_text(
        json.dumps(initial_pins, indent=2), encoding="utf-8"
    )
    from scripts.policy_owner.evidence_test_metrics import disk_metrics, memory_metrics
    from scripts.policy_owner.registered_test_fixtures import synthetic_stage_vectors
    from tests.fixtures.coverage_case import coverage_case
    from tests.fixtures.policy_coverage_fixture import (
        junction_buffer,
        make_coverage_fixture,
        seal_current,
    )
    from tradingagents.policy_coverage_models import CoverageEnvelope
    from tradingagents.policy_evidence_client import export_artifact, stage_artifact
    from tradingagents.policy_evidence_contract import MAX_DESCRIPTOR_BYTES, ArtifactDescriptor
    from tradingagents.policy_evidence_limits import bound_evidence_process
    from tradingagents.policy_qualified_contract import BytePin, canonical_bytes, content_identity

    bound_evidence_process(seconds=3660)
    timings = []

    def timed(label, fn, *call_args, **call_kwargs):
        start = time.monotonic()
        value = fn(*call_args, **call_kwargs)
        entry = {"step": label, "elapsed_seconds": time.monotonic() - start, **memory_metrics()}
        timings.append(entry)
        (out / "TIMINGS.json").write_text(json.dumps(timings, indent=2), encoding="utf-8")
        print(json.dumps(entry), flush=True)
        if (
            label
            in {"atomic_disposable_claim", "finalize", "separate_receipt", "downstream_report"}
            or label.startswith(("begin_", "complete_"))
        ) and entry["elapsed_seconds"] >= 300:
            raise TimeoutError(
                "real semantic fixture operation exceeded existing300s worker bound: " + label
            )
        return value

    f = make_coverage_fixture()
    root = r"D:\SYNTHETIC-HISTORY\build"
    rows = [
        {"path": ".", "kind": "directory"},
        {"path": "files", "kind": "directory"},
        {"path": "gaps", "kind": "directory"},
        {"path": "links", "kind": "directory"},
        {"path": "targets", "kind": "directory"},
    ]
    for i in range(7):
        name = f"gaps/gap-{i:02}"
        rows.extend(
            [
                {"path": name, "kind": "directory"},
                {
                    "path": name,
                    "kind": "unreadable_directory",
                    "attributes": 16,
                    "mtime_ns": 10 + i,
                },
            ]
        )
    for i in range(5322):
        rows.extend(
            [
                {"path": f"links/link-{i:05}", "kind": "reparse", "size": 0, "mtime_ns": 20 + i},
                {"path": f"targets/target-{i:05}", "kind": "directory"},
            ]
        )
    for i in range(args.file_count):
        digest = hashlib.sha256(("SYNTHETIC-DATA-" + str(i)).encode()).hexdigest()
        rows.append(
            {
                "path": f"files/{i:07}-{digest[:24]}.bin",
                "kind": "file",
                "byte_length": 1024 + i % 4096,
                "sha256": digest,
            }
        )
    rows.sort(key=lambda x: (x["path"].casefold(), x["kind"]))

    def originals(inventory_rows):
        result = {}
        for phase in ("before", "after"):
            r = {
                "path": root,
                "entries": inventory_rows,
                "file_count": args.file_count,
                "unreadable_count": 7,
                "security_descriptors_requested": False,
                "inventory_identity": content_identity(
                    "crash-protection-historical-root-inventory:v1", inventory_rows
                ),
            }
            body = {
                "phase": phase,
                "roots": [r],
                "file_count": args.file_count,
                "unreadable_count": 7,
                "elapsed_seconds": 1.0,
                "no_acl_write_or_recovery": True,
            }
            body["identity"] = content_identity("crash-protection-historical-inventory:v1", body)
            result[phase] = canonical_bytes(body) + b"\n"
        return result

    original = timed("generate_complete_original_shape_vectors", originals, rows)
    for phase, raw in original.items():
        (out / ("generated-original-" + phase + ".json")).write_bytes(raw)
    current = []
    for row in rows:
        if row["kind"] == "unreadable_directory":
            continue
        v = copy.deepcopy(row)
        v.update(
            attributes=1040
            if row["kind"] == "reparse"
            else 16
            if row["kind"] == "directory"
            else 32,
            ctime_ns=1,
            mtime_ns=row.get("mtime_ns", 1),
            descriptor_sha256=hashlib.sha256(("descriptor:" + row["path"]).encode()).hexdigest(),
        )
        if row["path"].startswith("gaps/gap-"):
            v["mtime_ns"] = 10 + int(row["path"].rsplit("-", 1)[1])
        current.append(v)
    for i in range(7):
        current.append(
            {
                "path": f"gaps/gap-{i:02}/newly-observed.bin",
                "kind": "file",
                "attributes": 32,
                "ctime_ns": 1,
                "mtime_ns": 1,
                "descriptor_sha256": "b" * 64,
                "byte_length": 3,
                "sha256": "a" * 64,
            }
        )
    current.sort(key=lambda x: x["path"].casefold())
    for i, row in enumerate(current):
        row["object_identity"] = {"volume_serial": "12345678", "file_index": f"{i + 1:016x}"}
    by_path = {r["path"]: r for r in current}
    for row in current:
        if row["kind"] == "reparse":
            target = "targets/target-" + row["path"].rsplit("-", 1)[1]
            row.update(
                tag=0xA0000003,
                reparse_data_base64=base64.b64encode(
                    junction_buffer(root + "\\" + target.replace("/", "\\"))
                ).decode(),
                target_relative_path=target,
                target_object_identity=by_path[target]["object_identity"],
                target_root_identity=by_path["."]["object_identity"],
            )
    f.update(original)
    f["baseline"]["roots"] = [
        {
            "path": root,
            "entries": current,
            "file_count": args.file_count + 7,
            "unreadable_count": 0,
            "security_descriptors_requested": True,
            "inventory_identity": "",
        }
    ]
    f["baseline"] = seal_current(f["baseline"])
    case_root = out / "case"
    case_root.mkdir()
    case = timed(
        "construct_scope_assessment_and_disk_fixture_authority",
        coverage_case,
        case_root,
        fixture=f,
        artifact_directory=out / "fixture-artifacts",
    )
    del f, rows, current, by_path, original
    gc.collect()
    assert len(case.coverage_scope.gaps) == 7 and len(case.coverage_scope.links) == 5322
    baseline_raw = case.owner.read_artifact(case.coverage.baseline)
    if len(baseline_raw) < 78489473:
        raise ValueError("new full inventory is below required retained scale")
    baseline_file = out / "baseline.json"
    baseline_file.write_bytes(baseline_raw)
    now = datetime.now(timezone.utc)
    descriptor = ArtifactDescriptor.seal(
        contract_version="policy-owner-artifact-descriptor-v2",
        operation="REGISTER_EVIDENCE",
        instance="test-20990101-999",
        role_identity=content_identity("synthetic-invalid-role:v1", "scale"),
        approval_identity=case.approval.approval_identity,
        candidate=case.candidate,
        lineage_plan_identity=case.policy.lineage_plan.lineage_plan_identity,
        acquisition_identity=case.coverage_baseline["capture"]["observation_identity"],
        stage="ISO02",
        phase="before",
        artifact_schema="policy-coverage-inventory:v2",
        artifact_identity_field="identity",
        artifact=case.coverage.baseline,
        issued_utc=now.isoformat(),
        expires_utc=(now + timedelta(hours=1)).isoformat(),
    )
    del baseline_raw
    gc.collect()
    store = out / "UNREGISTERED-NATIVE-STORE"
    for name in ("artifacts", "state/temp"):
        (store / name).mkdir(parents=True)
    (store / "UNAUTHENTICATED-TEST-STORE.txt").write_bytes(
        b"NO HUMAN AUTHENTICATION OR OPERATIONAL AUTHORITY\n"
    )
    wire = {"calls": 0, "request_bytes": 0, "response_bytes": 0, "maximum_request_bytes": 0}

    def call(operation, payload):
        raw = canonical_bytes({"operation": operation, "payload": payload})
        result = subprocess.run(
            [str(args.native_fixture), str(store), "test-20990101-999"],
            input=raw,
            capture_output=True,
            timeout=35,
            creationflags=subprocess.CREATE_NO_WINDOW,
            cwd=out,
        )
        wire["calls"] += 1
        wire["request_bytes"] += len(raw)
        wire["response_bytes"] += len(result.stdout)
        wire["maximum_request_bytes"] = max(wire["maximum_request_bytes"], len(raw))
        if result.returncode:
            raise ValueError(result.stderr.decode("utf-8", "replace"))
        return json.loads(result.stdout)

    staged = timed(
        "native_chunk_import",
        lambda: stage_artifact(
            call,
            baseline_file,
            descriptor,
            coverage_context=(case.coverage_scope, case.coverage_measurement),
            acquisition_registration=True,
        ),
    )
    timed(
        "native_complete_readback",
        lambda: export_artifact(call, descriptor.artifact, out / "readback.json"),
    )
    assert BytePin.of((out / "readback.json").read_bytes()) == descriptor.artifact.pin
    descriptors = [descriptor]
    transfer_receipts = [staged]
    for label in ("scope", "assessment"):
        reference = getattr(case.coverage, label)
        artifact_path = out / (label + ".json")
        artifact_path.write_bytes(case.owner.read_artifact(reference))
        data = descriptor.model_dump(mode="json", exclude={"identity"})
        data.update(
            artifact=reference.model_dump(mode="json"),
            artifact_schema=reference.identity.split(":sha256:")[0],
            acquisition_identity="GENERATED-TEST-metadata-" + reference.pin.sha256,
            phase="evidence",
        )
        extra = ArtifactDescriptor.seal(**data)
        descriptors.append(extra)
        transfer_receipts.append(
            timed("native_import_" + label, stage_artifact, call, artifact_path, extra)
        )
        target = out / (label + "-readback.json")
        timed("native_readback_" + label, export_artifact, call, reference, target)
        reference.pin.check(target.read_bytes())
    reviewed = timed(
        "native_full_transferred_descriptor_validation",
        lambda: call(
            "fixture-transferred-document",
            {
                "document": {
                    "contract_version": "policy-owner-evidence-intake-batch-v3",
                    "entries": transfer_receipts,
                    "execution_authorized": False,
                }
            },
        ),
    )
    if reviewed["complete_descriptors"] != 3 or reviewed["human_authenticated"] is not False:
        raise ValueError("native large descriptor inspection lost its non-authentication boundary")
    (out / "native-descriptor-review.json").write_bytes(canonical_bytes(reviewed) + b"\n")
    timed("atomic_disposable_claim", case.reserve)

    def step(kind):
        return {
            "REVIEW_STANDARDS": "REVIEWS",
            "REVIEW_SPEC": "REVIEWS",
            "PRE_RESULT_CLOSURE": "CLOSURE_HANDOFF",
        }.get(kind, kind)

    def before(kind):
        if kind != "REVIEW_SPEC":
            timed(
                "begin_" + step(kind),
                lambda: case.verifier.begin_stage(
                    case.policy,
                    case.approval,
                    case.claim,
                    stage=step(kind),
                    new_start_bridge=case.bridge,
                ),
            )

    def after(node):
        if node.kind != "REVIEW_STANDARDS":
            timed(
                "complete_" + step(node.kind),
                lambda: case.verifier.complete_stage(
                    case.policy, case.approval, case.claim, stage=step(node.kind), evidence=node
                ),
            )

    case.before_node = before
    case.after_node = after
    execution = timed(
        "all_synthetic_stage_vectors_and_real_validators", lambda: synthetic_stage_vectors(case)
    )
    result = timed(
        "finalize",
        lambda: case.verifier.finalize(
            policy=case.policy, approval=case.approval, claim=case.claim, execution=execution
        ),
    )
    case.owner.store(result, "result_identity")
    receipt = timed("separate_receipt", lambda: case.verifier.issue_receipt(result.to_bytes()))
    case.owner.store(receipt, "receipt_identity")
    envelope = CoverageEnvelope(
        contract_version="policy-qualified-downstream-envelope-v2",
        core=receipt.core,
        receipt=receipt,
        execution_authorized=False,
    )
    timed("downstream_report", lambda: case.verifier.report(envelope))
    # Pure compiled consumer validates the complete output framing/caveat, without
    # registration, a service, a child or a fabricated authentication result.
    native_consumer = args.native_fixture.with_name("CoverageEvidenceContracts.exe")
    native_raw = canonical_bytes(envelope.model_dump(mode="json"))
    native_path = out / "native-envelope.json"
    native_path.write_bytes(native_raw)

    def consume_native():
        checked = subprocess.run(
            [str(native_consumer), str(native_path), hashlib.sha256(native_raw).hexdigest()],
            capture_output=True,
            timeout=30,
            creationflags=subprocess.CREATE_NO_WINDOW,
        )
        (out / "native-consumer.stdout").write_bytes(checked.stdout)
        (out / "native-consumer.stderr").write_bytes(checked.stderr)
        if checked.returncode:
            raise ValueError("native full-route envelope rejected")

    timed("native_full_route_consumer", consume_native)
    (out / "result.json").write_bytes(result.to_bytes())
    (out / "receipt.json").write_bytes(receipt.to_bytes())
    (out / "envelope.json").write_bytes(envelope.to_bytes())
    if source_pins() != initial_pins:
        raise ValueError("executed source changed during full-scale proof")
    if native_pins() != initial_native_pins:
        raise ValueError("executed native fixture/build bytes changed during proof")
    metadata = [x.model_dump(mode="json") for x in descriptors]
    metadata_raw = canonical_bytes(metadata)
    (out / "descriptors.json").write_bytes(metadata_raw + b"\n")
    (out / "native-transfer-receipts.json").write_bytes(canonical_bytes(transfer_receipts) + b"\n")
    # Metadata capacity projection is explicitly not a genuine authenticated journal.
    # Include every fixture artifact once with maximum descriptor+auth overhead.
    route_count = len(case.owner.artifacts)
    journal_projection = route_count * (MAX_DESCRIPTOR_BYTES + 262144 + 1)
    if journal_projection > 33554432:
        raise ValueError("complete fixture route metadata exceeds retained journal capacity")
    timed("metadata_json_parse", lambda: json.loads(metadata_raw))
    report = {
        "status": "FIXTURE_FEASIBILITY_PASS",
        "source_pins": initial_pins,
        "native_execution_pins": initial_native_pins,
        "generated_records_not_native_objects": True,
        "actual_native_capture_count": 0,
        "gap_markers": 7,
        "historical_reparse_records": 5322,
        "baseline_bytes": descriptor.artifact.pin.byte_length,
        "assessment_bytes": case.coverage.assessment.pin.byte_length,
        "scope_bytes": case.coverage.scope.pin.byte_length,
        "descriptor_bytes": len(descriptor.to_bytes()),
        "complete_transferred_bytes": sum(x.artifact.pin.byte_length for x in descriptors),
        "fixture_artifact_count": route_count,
        "conservative_journal_projection_bytes": journal_projection,
        "projection_is_not_authentication": True,
        "wire": wire,
        "resources": {**memory_metrics(), **disk_metrics(out)},
        "timings": timings,
        "qualification": False,
        "human_authentication": False,
        "execution_authorized": False,
    }
    (out / "SCALE-RESULTS.json").write_bytes(canonical_bytes(report) + b"\n")
    print(json.dumps(report), flush=True)


if __name__ == "__main__":
    main()

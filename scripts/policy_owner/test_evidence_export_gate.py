"""Measure actual full-reference owner read gate plus native chunks on a retained TEST capture.

This reuses an explicitly labelled disposable observation without recapturing or
changing its identity/time. Invalid synthetic authentication is the sole test
boundary. It does not measure service startup, genuine signatures or owner RPC.
"""

from __future__ import annotations

import argparse
import json
import os
import runpy
import subprocess
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ("output", "capture", "native-fixture", "dependencies"):
        parser.add_argument("--" + name, type=Path, required=True)
    args = parser.parse_args()
    source = Path(__file__).resolve().parents[2]
    output = args.output.resolve()
    if (
        output.exists()
        or output.is_relative_to(source)
        or output.is_relative_to(Path(os.environ["PROGRAMDATA"]))
    ):
        raise ValueError("fresh external fixture output required")
    sys.path[:0] = [str(source), str(args.dependencies), str(Path(__file__).resolve().parent)]
    from evidence_test_metrics import disk_metrics, memory_metrics

    from tradingagents.policy_evidence_acquisition import validate_capture_provenance
    from tradingagents.policy_evidence_client import export_artifact, stage_artifact
    from tradingagents.policy_evidence_contract import ArtifactDescriptor
    from tradingagents.policy_evidence_limits import bound_evidence_process
    from tradingagents.policy_qualified_contract import BytePin, EvidenceRef, canonical_bytes

    bound_evidence_process(seconds=600)
    raw = args.capture.read_bytes()
    body = json.loads(raw)
    provenance = validate_capture_provenance(body)
    if provenance["fixture_only"] is not True:
        raise ValueError("only an existing disposable TEST capture is allowed")
    output.mkdir()
    boundary = runpy.run_path(str(source / "tests/fixtures/policy_owner/local_owner_fixture.py"))[
        "SyntheticNativeBoundary"
    ](output / "INVALID-AUTH-BOUNDARY")
    now = datetime.now(timezone.utc)
    descriptor = ArtifactDescriptor.seal(
        contract_version="policy-owner-artifact-descriptor-v2",
        operation="REGISTER_EVIDENCE",
        instance=boundary.owner.anchor["instance"],
        role_identity=boundary.role.identity,
        approval_identity=boundary.approval.approval_identity,
        candidate=boundary.approval.candidate,
        lineage_plan_identity=boundary.approval.lineage_plan_identity,
        acquisition_identity=provenance["observation_identity"],
        stage="ISO02",
        phase=body["phase"],
        artifact_schema="crash-protection-historical-inventory:v1",
        artifact_identity_field="identity",
        artifact=EvidenceRef(identity=body["identity"], pin=BytePin.of(raw)),
        issued_utc=now.isoformat(),
        expires_utc=(now + timedelta(hours=1)).isoformat(),
    )
    del body, raw
    gates = []
    calls = 0

    def call(operation, payload):
        nonlocal calls
        if operation == "evidence-read-chunk-v2":
            started = time.monotonic()
            complete = boundary.owner.read_artifact(EvidenceRef.read(payload["reference"]))
            gates.append(
                {"bytes_read_and_hashed": len(complete), "seconds": time.monotonic() - started}
            )
            del complete
        result = subprocess.run(
            [str(args.native_fixture), str(boundary.owner.root), boundary.owner.anchor["instance"]],
            input=canonical_bytes({"operation": operation, "payload": payload}),
            capture_output=True,
            timeout=35,
            creationflags=subprocess.CREATE_NO_WINDOW,
            cwd=output,
        )
        calls += 1
        if result.returncode:
            raise ValueError(result.stderr.decode("utf-8", "replace"))
        return json.loads(result.stdout)

    intake = stage_artifact(call, args.capture, descriptor)
    boundary.apply("REGISTER_EVIDENCE", intake)
    started = time.monotonic()
    result = export_artifact(call, descriptor.artifact, output / "readback.json")
    elapsed = time.monotonic() - started
    report = {
        "status": "PASS_COMPLETE_REGISTERED_REFERENCE_GATE_AND_NATIVE_EXPORT_FIXTURE",
        "artifact": descriptor.artifact.model_dump(mode="json"),
        "source_capture": str(args.capture),
        "observation_identity_preserved": provenance["observation_identity"],
        "new_capture_performed": False,
        "export_seconds": elapsed,
        "native_calls": calls,
        "per_chunk_complete_owner_checks": len(gates),
        "owner_bytes_read_and_hashed_total": sum(item["bytes_read_and_hashed"] for item in gates),
        "owner_gate_seconds_total": sum(item["seconds"] for item in gates),
        "gates": gates,
        "export": result,
        "memory": memory_metrics(),
        "disk": disk_metrics(output),
        "service_or_native_signature_verification_measured": False,
        "native_authentication": False,
        "real_claim": False,
        "execution_authorized": False,
    }
    (output / "result.json").write_bytes(canonical_bytes(report))
    print(
        json.dumps({key: value for key, value in report.items() if key not in {"gates", "export"}})
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

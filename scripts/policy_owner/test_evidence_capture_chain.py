"""Actual disposable capture -> native bytes -> synthetic intake -> real claim validator.

The native authentication boundary is deliberately invalid test data. Neither
this program nor its records can pass production native authentication. No
installed owner, original historical root, or genuine claim is accessed.
"""

from __future__ import annotations

import argparse
import dataclasses
import hashlib
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
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--dependencies", type=Path, required=True)
    parser.add_argument("--native-fixture", type=Path, required=True)
    parser.add_argument("--files", type=int, default=10)
    parser.add_argument("--minimum-bytes", type=int, default=0)
    parser.add_argument("--reuse-before", type=Path)
    parser.add_argument("--reuse-after", type=Path)
    args = parser.parse_args()
    source = Path(__file__).resolve().parents[2]
    output = args.output.resolve()
    if (
        output.exists()
        or output.is_relative_to(source)
        or output.is_relative_to(Path(os.environ["PROGRAMDATA"]))
    ):
        raise ValueError("fresh external fixture output required")
    if not 1 <= args.files <= 300000 or args.native_fixture.name != "EvidenceTransferContracts.exe":
        raise ValueError("bounded explicit headless fixture required")
    if (args.reuse_before is None) != (args.reuse_after is None):
        raise ValueError("both exact disposable capture paths required for a consumer-only rerun")
    sys.path[:0] = [str(source), str(args.dependencies), str(Path(__file__).resolve().parent)]
    from evidence_test_metrics import disk_metrics, memory_metrics

    from tradingagents.policy_evidence_client import export_artifact, stage_artifact
    from tradingagents.policy_evidence_contract import ArtifactDescriptor, validate_artifact
    from tradingagents.policy_evidence_limits import bound_evidence_process
    from tradingagents.policy_qualified_contract import (
        BytePin,
        LineageAllocation,
        RawComparison,
        canonical_bytes,
    )
    from tradingagents.policy_qualified_results import (
        ObservationResolution,
        PolicyQualifiedVerifier,
    )

    bound_evidence_process(seconds=1800)
    output.mkdir()
    unrelated = output / "unrelated-cwd"
    unrelated.mkdir()
    measured = output / "NEW-DISPOSABLE-ROOT"
    if not args.reuse_before:
        measured.mkdir()
    timings, commands = [], []

    def timed(name, function):
        start = time.monotonic()
        result = function()
        item = {"step": name, "seconds": time.monotonic() - start, **memory_metrics()}
        timings.append(item)
        (output / "timings.json").write_text(json.dumps(timings, indent=2), encoding="utf-8")
        print(json.dumps(item), flush=True)
        return result

    def generate():
        for index in range(args.files):
            # Actual non-zero unpredictable bytes; resident files minimize host disk use.
            (measured / f"fixture-{index:08x}.dat").write_bytes(os.urandom(48))

    if not args.reuse_before:
        timed("create_actual_disposable_files", generate)
    roots = output / "roots.json"
    roots.write_bytes(
        canonical_bytes(
            {
                "contract_version": "policy-evidence-root-list-v1",
                "fixture_only": True,
                "roots": [{"path": str(measured), "security_descriptors_requested": True}],
            }
        )
    )
    cli = [
        sys.executable,
        "-I",
        "-B",
        "-S",
        str(source / "scripts/policy_owner/acquire_evidence.py"),
        "--development-dependencies",
        str(args.dependencies),
        "--fixture-only",
    ]

    def run_cli(*arguments, expected=0):
        command = [*cli, *map(str, arguments)]
        result = subprocess.run(
            command,
            cwd=unrelated,
            capture_output=True,
            timeout=650,
            creationflags=subprocess.CREATE_NO_WINDOW,
        )
        commands.append(
            {
                "argv": command,
                "exit": result.returncode,
                "stdout": result.stdout.decode("utf-8", "replace"),
                "stderr": result.stderr.decode("utf-8", "replace"),
            }
        )
        (output / "commands.json").write_text(json.dumps(commands, indent=2), encoding="utf-8")
        assert result.returncode == expected, commands[-1]

    before, after = (
        args.reuse_before or output / "before.json",
        args.reuse_after or output / "after.json",
    )
    if args.reuse_before:
        from tradingagents.policy_evidence_acquisition import validate_capture_provenance

        for path in (before, after):
            capture = json.loads(path.read_bytes())
            if validate_capture_provenance(capture)["fixture_only"] is not True:
                raise ValueError(
                    "consumer regression accepts only existing labelled disposable observations"
                )
    else:
        timed(
            "actual_capture_before_cli",
            lambda: run_cli(
                "capture", "--root-list", roots, "--output", before, "--phase", "before"
            ),
        )
        timed(
            "actual_capture_after_cli",
            lambda: run_cli(
                "capture",
                "--root-list",
                roots,
                "--output",
                after,
                "--phase",
                "after",
                "--previous",
                before,
            ),
        )
    timed(
        "actual_full_comparison_cli",
        lambda: run_cli(
            "compare", "--before", before, "--after", after, "--output", output / "comparison.json"
        ),
    )
    before_body = json.loads(before.read_bytes())
    assert (
        before.stat().st_size >= args.minimum_bytes and before_body["capture"]["complete"] is True
    )
    factory = runpy.run_path(str(source / "tests/fixtures/policy_owner/local_owner_fixture.py"))[
        "SyntheticNativeBoundary"
    ]
    boundary = timed(
        "construct_explicit_synthetic_authority_boundary",
        lambda: factory(output / "BOUNDARY", root_template=before_body["roots"][0]),
    )
    calls = {"native_calls": 0, "maximum_request_bytes": 0, "request_bytes": 0, "response_bytes": 0}

    def native(operation, payload):
        if operation == "evidence-read-chunk-v2":
            # The production service performs this owner gate before native readback.
            # Here the same real registration gate follows INVALID fixture authentication.
            from tradingagents.policy_qualified_contract import EvidenceRef

            boundary.owner.read_artifact(EvidenceRef.read(payload["reference"]))
        raw = canonical_bytes({"operation": operation, "payload": payload})
        result = subprocess.run(
            [str(args.native_fixture), str(boundary.owner.root), boundary.owner.anchor["instance"]],
            input=raw,
            capture_output=True,
            timeout=35,
            cwd=unrelated,
            creationflags=subprocess.CREATE_NO_WINDOW,
        )
        calls["native_calls"] += 1
        calls["request_bytes"] += len(raw)
        calls["response_bytes"] += len(result.stdout)
        calls["maximum_request_bytes"] = max(calls["maximum_request_bytes"], len(raw))
        if result.returncode:
            raise ValueError(result.stderr.decode("utf-8", "replace"))
        return json.loads(result.stdout)

    now = datetime.now(timezone.utc)
    descriptor = ArtifactDescriptor.seal(
        contract_version="policy-owner-artifact-descriptor-v2",
        operation="REGISTER_EVIDENCE",
        instance=boundary.owner.anchor["instance"],
        role_identity=boundary.role.identity,
        approval_identity=boundary.approval.approval_identity,
        candidate=boundary.approval.candidate,
        lineage_plan_identity=boundary.approval.lineage_plan_identity,
        acquisition_identity=before_body["capture"]["observation_identity"],
        stage="ISO02",
        phase="before",
        artifact_schema="crash-protection-historical-inventory:v1",
        artifact_identity_field="identity",
        artifact={"identity": before_body["identity"], "pin": BytePin.of(before.read_bytes())},
        issued_utc=now.isoformat(),
        expires_utc=(now + timedelta(hours=1)).isoformat(),
    )
    intake = timed("real_native_stage", lambda: stage_artifact(native, before, descriptor))
    try:
        boundary.owner.read_artifact(descriptor.artifact)
    except ValueError:
        pass
    else:
        raise AssertionError("staged bytes became registered without even the fixture gate")
    # A production worker receives a fresh native request clock. Refresh the
    # disposable owner's real clock after staging, rather than its constructor clock.
    boundary.owner.fresh_time()
    window = timed(
        "private_semantic_validation_returns_exact_validity_window",
        lambda: boundary.owner.authentication_validation_window("REGISTER_EVIDENCE", intake),
    )
    native(
        "fixture-validation-window",
        {
            "validation": window,
            "purpose": "REGISTER_EVIDENCE",
            "document_sha256": window["document_sha256"],
            "at_utc": datetime.now(timezone.utc).isoformat(),
        },
    )
    timed(
        "real_descriptor_validation_under_invalid_fixture_authentication",
        lambda: boundary.apply("REGISTER_EVIDENCE", intake),
    )
    readback = output / "readback.json"
    timed(
        "registered_reference_native_chunk_readback",
        lambda: export_artifact(native, descriptor.artifact, readback),
    )
    retrieved = timed(
        "complete_retrieved_artifact_validation", lambda: validate_artifact(readback, descriptor)
    )
    assert PolicyQualifiedVerifier._roots(before_body) == PolicyQualifiedVerifier._roots(retrieved)
    del retrieved
    boundary.apply(
        "REGISTER_EVIDENCE",
        {
            "contract_version": "policy-owner-evidence-intake-v1",
            "kind": "current-source",
            "key": "current",
            "value": {
                "candidate": boundary.approval.candidate.model_dump(mode="json"),
                "lineage_plan_identity": boundary.approval.lineage_plan_identity,
                "production_inventory_identity": "production-python-modules:v1:sha256:" + "d" * 64,
            },
        },
    )
    # TEST-only v1 source authority here; real source producer/proof is tested separately.
    allocation_data = boundary.case.allocation.model_dump(mode="json")
    allocation_data.pop("allocation_identity")
    allocation_data["approval_identity"] = boundary.approval.approval_identity
    allocation = LineageAllocation.seal(**allocation_data)
    boundary.apply(
        "REGISTER_EVIDENCE",
        {
            "contract_version": "policy-owner-evidence-intake-v1",
            "kind": "allocation",
            "key": allocation.lineage_plan.lineage_plan_identity,
            "value": allocation.model_dump(mode="json"),
        },
    )
    observation = ObservationResolution(
        descriptor.acquisition_identity,
        descriptor.artifact,
        boundary.policy.lineage_plan.measurement_contract_pin,
        None,
        boundary.approval.lineage_plan_identity,
        allocation.allocation_identity,
        "ISO02",
        "before",
        "NEW_QUALIFICATION",
        before_body["capture"]["finished_utc"],
    )
    value = dataclasses.asdict(observation)
    value["artifact"] = descriptor.artifact.model_dump(mode="json")
    value["measurement_contract_pin"] = (
        boundary.policy.lineage_plan.measurement_contract_pin.model_dump(mode="json")
    )
    boundary.apply(
        "REGISTER_EVIDENCE",
        {
            "contract_version": "policy-owner-evidence-intake-v2",
            "kind": "observation",
            "key": descriptor.acquisition_identity,
            "value": value,
        },
    )
    bridge = RawComparison.seal(
        contract_version="policy-qualified-raw-comparison-v1",
        measurement_contract_pin=boundary.policy.lineage_plan.measurement_contract_pin,
        before_observation=boundary.policy.scope.after.observation_id,
        after_observation=descriptor.acquisition_identity,
        before=boundary.policy.scope.after.artifact,
        after=descriptor.artifact,
        comparable=True,
        equal=True,
    )
    verifier = PolicyQualifiedVerifier(owner=boundary.owner, ledger=boundary.owner.ledger)
    claim = timed(
        "actual_atomic_admission_in_disposable_synthetic_store",
        lambda: verifier.claim(
            boundary.policy.to_bytes(),
            boundary.approval.to_bytes(),
            allocation.to_bytes(),
            new_start_bridge=bridge.to_bytes(),
        ),
    )
    try:
        verifier.claim(
            boundary.policy.to_bytes(),
            boundary.approval.to_bytes(),
            allocation.to_bytes(),
            new_start_bridge=bridge.to_bytes(),
        )
    except ValueError:
        replay_rejected = True
    else:
        raise AssertionError("disposable fixture claim replay accepted")
    report = {
        "status": "PASS_ACTUAL_CAPTURE_NATIVE_INTAKE_FIXTURE_ADMISSION",
        "timings": timings,
        "native": calls,
        "actual_files": before_body["file_count"],
        "new_capture_performed": args.reuse_before is None,
        "capture_inputs": {"before": str(before), "after": str(after)},
        "artifact_bytes": before.stat().st_size,
        "artifact_sha256": hashlib.sha256(before.read_bytes()).hexdigest(),
        "fixture_claim": claim.model_dump(mode="json"),
        "replay_rejected": replay_rejected,
        "journal_document_bytes": len(canonical_bytes(boundary.owner.records)),
        "memory": memory_metrics(),
        "disk": disk_metrics(output),
        "native_authentication": False,
        "test_invalid_authentication_boundary": True,
        "source_authority_in_this_chain": "TEST_V1_SYNTHETIC; real producer tested separately",
        "real_qualification": False,
        "execution_authorized": False,
    }
    (output / "result.json").write_text(json.dumps(report, indent=2), encoding="utf-8")
    print(
        json.dumps(
            {
                "status": report["status"],
                "artifact_bytes": report["artifact_bytes"],
                "memory": report["memory"],
                "disk": report["disk"],
            }
        ),
        flush=True,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

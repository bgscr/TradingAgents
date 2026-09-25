"""Real six-stage validators/finalizer on declared synthetic evidence, never ISO execution."""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dependencies", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--root-template", type=Path)
    args = parser.parse_args()
    source = Path(__file__).resolve().parents[2]
    output = args.output.resolve()
    if (
        output.exists()
        or output.is_relative_to(source)
        or output.is_relative_to(Path(os.environ["PROGRAMDATA"]))
    ):
        raise ValueError("fresh external disposable stage fixture required")
    sys.path[:0] = [
        str(source),
        str(args.dependencies.resolve(strict=True)),
        str(Path(__file__).resolve().parent),
    ]
    from evidence_test_metrics import disk_metrics, memory_metrics
    from predeployment_fixtures import SyntheticCase
    from registered_test_fixtures import synthetic_stage_vectors

    from tradingagents.policy_evidence_contract import ArtifactDescriptor, validate_artifact
    from tradingagents.policy_evidence_limits import bound_evidence_process
    from tradingagents.policy_qualified_contract import BytePin, DownstreamEnvelope, EvidenceRef

    bound_evidence_process(seconds=1800)
    output.mkdir()
    template = None
    if args.root_template:
        # Generated/readback schema corpus only. Never use a retained historical
        # corpus as a new observation or synthetic replacement original.
        body = json.loads(args.root_template.read_bytes())
        if body.get("fixture_kind") != "SYNTHETIC_SCHEMA_VECTOR_NOT_AN_ACQUISITION":
            raise ValueError("only labelled generated schema vectors can extend this fixture")
        template = body["roots"][0]
    timings = []
    schema_checks = []

    def check_ref(case, reference, stage):
        raw = case.owner.read_artifact(reference)
        value = json.loads(raw)
        fields = [key for key, item in value.items() if item == reference.identity]
        assert len(fields) == 1, (reference.identity, fields)
        now = datetime.now(timezone.utc)
        descriptor = ArtifactDescriptor.seal(
            contract_version="policy-owner-artifact-descriptor-v2",
            operation="REGISTER_EVIDENCE",
            instance="test-20300101-001",
            role_identity="policy-owner-project-role-proposal:v1:sha256:" + "1" * 64,
            approval_identity=case.approval.approval_identity,
            candidate=case.candidate,
            lineage_plan_identity=case.policy.lineage_plan.lineage_plan_identity,
            acquisition_identity="SYNTHETIC-SCHEMA-CHECK",
            stage=stage,
            phase=value.get("phase", "evidence"),
            artifact_schema=reference.identity.split(":sha256:")[0],
            artifact_identity_field=fields[0],
            artifact=reference,
            issued_utc=now.isoformat(),
            expires_utc=(now + timedelta(hours=1)).isoformat(),
        )
        path = output / ("schema-" + reference.pin.sha256 + ".json")
        if not path.exists():
            path.write_bytes(raw)
        validate_artifact(path, descriptor)
        schema_checks.append(
            {
                "schema": descriptor.artifact_schema,
                "stage": stage,
                "bytes": len(raw),
                "descriptor_bytes": len(descriptor.to_bytes()),
                "sha256": reference.pin.sha256,
            }
        )
        (output / "schema-checks.json").write_text(
            json.dumps(schema_checks, indent=2), encoding="utf-8"
        )

    def observed(name, function):
        started = time.monotonic()
        result = function()
        record = {"step": name, "elapsed_seconds": time.monotonic() - started, **memory_metrics()}
        timings.append(record)
        (output / "timings.json").write_text(json.dumps(timings, indent=2), encoding="utf-8")
        print(json.dumps(record), flush=True)
        return result

    case_root = output / "SYNTHETIC-CASE"
    case_root.mkdir()

    class StageCase(SyntheticCase):
        @staticmethod
        def stage(kind):
            return {
                "REVIEW_STANDARDS": "REVIEWS",
                "REVIEW_SPEC": "REVIEWS",
                "PRE_RESULT_CLOSURE": "CLOSURE_HANDOFF",
            }.get(kind, kind)

        def before_node(self, kind):
            if kind != "REVIEW_SPEC":
                stage = self.stage(kind)
                observed(
                    "begin_" + stage,
                    lambda: self.verifier.begin_stage(
                        self.policy,
                        self.approval,
                        self.claim,
                        stage=stage,
                        new_start_bridge=self.bridge,
                    ),
                )

        def after_node(self, node):
            for reference in node.artifacts:
                check_ref(self, reference, self.stage(node.kind))
            check_ref(self, self.owner.store(node), self.stage(node.kind))
            if node.kind != "REVIEW_STANDARDS":
                stage = self.stage(node.kind)
                observed(
                    "complete_" + stage,
                    lambda: self.verifier.complete_stage(
                        self.policy, self.approval, self.claim, stage=stage, evidence=node
                    ),
                )

    case = observed(
        "construct_synthetic_authority_and_full_inventory_vectors",
        lambda: StageCase(case_root, root_template=template),
    )
    case.reserve()
    execution = observed(
        "construct_synthetic_stage_vectors_not_execute_stages",
        lambda: synthetic_stage_vectors(case),
    )
    result = observed(
        "finalize_conditional_fixture_result",
        lambda: case.verifier.finalize(
            policy=case.policy, approval=case.approval, claim=case.claim, execution=execution
        ),
    )
    case.owner.store(result, "result_identity")
    receipt = observed(
        "separate_fixture_receipt", lambda: case.verifier.issue_receipt(result.to_bytes())
    )
    check_ref(
        case,
        EvidenceRef(identity=result.result_identity, pin=BytePin.of(result.to_bytes())),
        "CLOSURE_HANDOFF",
    )
    check_ref(case, case.owner.store(receipt, "receipt_identity"), "CLOSURE_HANDOFF")
    envelope = DownstreamEnvelope(
        contract_version="policy-qualified-downstream-envelope-v1",
        core=receipt.core,
        receipt=receipt,
        execution_authorized=False,
    )
    observed("conditional_fixture_consumer", lambda: case.verifier.verify_downstream(envelope))
    assert (
        result.status == "CONDITIONALLY_ACCEPTED_UNRESOLVED_HISTORY"
        and result.history.historical_equal is False
    )
    try:
        case.reserve()
    except ValueError:
        replay_rejected = True
    else:
        raise AssertionError("synthetic consumed claim was replayable")
    report = {
        "status": "PASS_SIX_STAGE_FIXTURE_INTERFACES_NOT_QUALIFICATION",
        "stage_count": 6,
        "timings": timings,
        "schema_checks": schema_checks,
        "disk": disk_metrics(output),
        "memory": memory_metrics(),
        "fixture_result_identity": result.result_identity,
        "fixture_receipt_identity": receipt.receipt_identity,
        "historical_equal": False,
        "historical_cause": "UNRESOLVED",
        "replay_rejected": replay_rejected,
        "actual_iso_or_sandbox_executed": False,
        "native_authentication": False,
        "real_claim_or_approval": False,
        "execution_authorized": False,
    }
    (output / "result.json").write_text(json.dumps(report, indent=2), encoding="utf-8")
    print(
        json.dumps(
            {"status": report["status"], "memory": report["memory"], "disk": report["disk"]}
        ),
        flush=True,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

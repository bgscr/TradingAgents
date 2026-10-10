"""Actual LocalWindowsPolicyOwner at the existing invalid-native-record TEST seam."""

import json
from datetime import datetime, timedelta, timezone

import pytest

from tests.fixtures.policy_owner.local_owner_fixture import SyntheticNativeBoundary
from tests.unit.test_policy_coverage_native import native_fixture
from tradingagents.policy_coverage_contract import CoverageMeasurement, artifact_ref, build_scope
from tradingagents.policy_coverage_models import (
    CoverageApproval,
    CoverageDecisionProposal,
    CoveragePolicy,
)
from tradingagents.policy_coverage_native import capture_coverage
from tradingagents.policy_evidence_contract import ArtifactDescriptor
from tradingagents.policy_qualified_contract import BytePin, LineagePlan, ReviewedPolicyPackage


def owner_case(tmp_path):
    _root, old, _scope, _measurement = native_fixture(tmp_path)
    b = SyntheticNativeBoundary(
        tmp_path / "boundary", root_template=json.loads(old["after"])["roots"][0]
    )
    scope = build_scope(
        b.owner.read_artifact(b.policy.scope.before.artifact),
        b.owner.read_artifact(b.policy.scope.after.artifact),
        fixture_only=True,
    )
    m = _measurement.model_dump(mode="json")
    m.pop("identity")
    m.update(scope=artifact_ref(scope), candidate=b.approval.candidate)
    measurement = CoverageMeasurement.seal(**m)
    plan = b.policy.lineage_plan.model_dump(mode="json")
    plan.pop("lineage_plan_identity")
    plan["measurement_contract_pin"] = BytePin.of(measurement.to_bytes())
    plan = LineagePlan.seal(**plan)
    p = b.policy.model_dump(mode="json")
    p.pop("acceptance_policy_identity")
    p.update(
        contract_version="historical-state-acceptance-policy-v2",
        lineage_plan=plan,
        coverage_scope=artifact_ref(scope),
        coverage_measurement=measurement,
        coverage_rule="KNOWN_RECORDED_FIELDS_UNKNOWN_HISTORY_STRICT_CURRENT_WINDOW",
    )
    policy = CoveragePolicy.seal(**p)
    # Trusted fixture setup only. Real policy bytes require supported human intake.
    policy_ref = b.owner.put(policy.to_bytes(), policy.acceptance_policy_identity)
    package = ReviewedPolicyPackage.seal(
        contract_version="reviewed-policy-contract-package-v1",
        policy=policy_ref,
        scope_evidence_manifest_pin=policy.scope_evidence_manifest_pin,
        contract_source_pin=plan.consumer_contract_pin,
    )
    proposal = dict(b.owner.records[-1]["document"])
    proposal.pop("identity")
    proposal.update(
        contract_version="policy-owner-exact-decision-proposal-v2",
        policy=policy,
        reviewed_package=package,
        coverage_adoption="PROSPECTIVE_ONLY_UNKNOWN_HISTORY_RETAINED",
        coverage_risk_selection="ACCEPT_PERSISTENT_HISTORICAL_COVERAGE_UNKNOWN",
    )
    proposal["execution"] = dict(proposal["execution"])
    proposal["execution"]["lineage_plan_identity"] = plan.lineage_plan_identity
    proposal = CoverageDecisionProposal.seal(**proposal)
    b.old_approval = b.approval
    response = b.apply(
        "EXACT_POLICY_DECISION", proposal.model_dump(mode="json"), at=b.now - timedelta(minutes=30)
    )
    b.approval = CoverageApproval.read(response["approval"])
    b.policy = policy
    path = tmp_path / "new-capture.json"
    value = capture_coverage(
        scope,
        measurement,
        path,
        instance=b.owner.anchor["instance"],
        phase="before",
        fixture_only=True,
    )
    assert value["capture"]["complete"] is True
    now = datetime.now(timezone.utc)
    descriptor = ArtifactDescriptor.seal(
        contract_version="policy-owner-artifact-descriptor-v2",
        operation="REGISTER_EVIDENCE",
        instance=b.owner.anchor["instance"],
        role_identity=b.role.identity,
        approval_identity=b.approval.approval_identity,
        candidate=b.approval.candidate,
        lineage_plan_identity=plan.lineage_plan_identity,
        acquisition_identity=value["capture"]["observation_identity"],
        stage="ISO02",
        phase="before",
        artifact_schema="policy-coverage-inventory:v2",
        artifact_identity_field="identity",
        artifact={"identity": value["identity"], "pin": BytePin.of(path.read_bytes())},
        issued_utc=now.isoformat(),
        expires_utc=(now + timedelta(minutes=10)).isoformat(),
    )
    entry = b.seed_contract_bytes(descriptor, path.read_bytes())
    b.owner.now_utc = now.isoformat()
    return b, scope, measurement, value, descriptor, entry


def test_versioned_decision_derives_scope_then_compact_native_fixture_intake(tmp_path):
    b, scope, measurement, value, descriptor, entry = owner_case(tmp_path)
    assert b.owner.read_artifact(artifact_ref(scope)) == scope.to_bytes()
    assert b.approval.coverage_scope == artifact_ref(scope)
    assert b.approval.approval_identity != b.old_approval.approval_identity
    b.owner.validate_authentication_document("REGISTER_EVIDENCE", entry)
    b.apply("REGISTER_EVIDENCE", entry)
    assert BytePin.of(b.owner.read_artifact(descriptor.artifact)) == descriptor.artifact.pin
    with pytest.raises(ValueError):
        b.owner.validate_authentication_document("REGISTER_EVIDENCE", entry)


@pytest.mark.parametrize("field", ["scope", "candidate", "instance", "approval"])
def test_wrong_versioned_intake_context_rejects_before_authentication(tmp_path, field):
    b, scope, measurement, value, descriptor, entry = owner_case(tmp_path)
    payload = descriptor.model_dump(mode="json")
    payload.pop("identity")
    if field == "instance":
        payload["instance"] = "test-20990101-111"
    elif field == "candidate":
        payload["candidate"]["commit"] = "0" * 40
    elif field == "approval":
        payload["approval_identity"] = b.old_approval.approval_identity
    else:
        payload["lineage_plan_identity"] = "historical-state-lineage-plan:v1:sha256:" + "0" * 64
    bad = ArtifactDescriptor.seal(**payload)
    raw = (b.owner.root / "artifacts" / (descriptor.artifact.pin.sha256 + ".blob")).read_bytes()
    altered = b.seed_contract_bytes(bad, raw)
    before = len(b.owner.records)
    with pytest.raises(ValueError):
        b.owner.validate_authentication_document("REGISTER_EVIDENCE", altered)
    assert len(b.owner.records) == before

"""Distinct acquisitions of identical canonical state; INVALID fixture authority."""

from dataclasses import asdict
from datetime import datetime, timedelta, timezone

import pytest

from scripts.policy_owner.predeployment_fixtures import canonical_model
from tests.unit.test_policy_coverage_owner import owner_case
from tradingagents.policy_evidence_contract import ArtifactDescriptor
from tradingagents.policy_owner_contract import evidence_intake_entries
from tradingagents.policy_qualified_contract import (
    BytePin,
    EvidenceRef,
    canonical_bytes,
    content_identity,
)
from tradingagents.policy_qualified_results import ObservationResolution
from tradingagents.windows_sandbox_qualification import (
    ProtectedHostInventory,
    ProtectedObjectKind,
    ProtectedRootInventory,
    ProtectedRootRole,
)


def host_bytes():
    roots = [
        canonical_model(
            ProtectedRootInventory,
            {
                "role": role.value,
                "object_kind": ProtectedObjectKind.DIRECTORY.value,
                "root_path_identity": "host-path:sha256:" + "1" * 64,
                "file_count": 0,
                "byte_length": 0,
                "files": [],
            },
            "windows-sandbox-protected-root-inventory:v1",
            "inventory_identity",
        ).model_dump(mode="json")
        for role in ProtectedRootRole
    ]
    host = canonical_model(
        ProtectedHostInventory,
        {
            "protected_root_manifest_identity": "SYNTHETIC",
            "immutable_verifier_identity": "SYNTHETIC",
            "roots": roots,
            "files": [],
            "file_count": 0,
            "byte_length": 0,
            "semantic_inventory": {"SYNTHETIC": "unchanged"},
            "semantic_inventory_identity": content_identity(
                "ticket06-immutable-semantic-inventory:v1", {"SYNTHETIC": "unchanged"}
            ),
        },
        "windows-sandbox-protected-host-inventory:v1",
        "inventory_identity",
    )
    raw = canonical_bytes(host.model_dump(mode="json")) + b"\n"
    return raw, EvidenceRef(identity=host.inventory_identity, pin=BytePin.of(raw))


def descriptor(b, ref, name, at):
    return ArtifactDescriptor.seal(
        contract_version="policy-owner-artifact-descriptor-v2",
        operation="REGISTER_EVIDENCE",
        instance=b.owner.anchor["instance"],
        role_identity=b.role.identity,
        approval_identity=b.approval.approval_identity,
        candidate=b.approval.candidate,
        lineage_plan_identity=b.policy.lineage_plan.lineage_plan_identity,
        acquisition_identity=name,
        stage="CLOSURE_HANDOFF",
        phase="after",
        artifact_schema="windows-sandbox-protected-host-inventory:v1",
        artifact_identity_field="inventory_identity",
        artifact=ref,
        issued_utc=at.isoformat(),
        expires_utc=(at + timedelta(minutes=5)).isoformat(),
    )


def entry(b, d, raw):
    value = b.seed_contract_bytes(d, raw)
    return {
        **value,
        "contract_version": "policy-owner-evidence-intake-v3",
        "kind": "artifact-acquisition",
        "key": d.identity,
    }


def observation(b, d, at):
    value = asdict(
        ObservationResolution(
            d.acquisition_identity,
            d.artifact,
            b.policy.lineage_plan.measurement_contract_pin,
            "SYNTHETIC-lineage",
            d.lineage_plan_identity,
            "SYNTHETIC-allocation",
            d.stage,
            d.phase,
            "NEW_QUALIFICATION",
            at.isoformat(),
        )
    )
    value["artifact"] = d.artifact.model_dump(mode="json")
    value["measurement_contract_pin"] = b.policy.lineage_plan.measurement_contract_pin.model_dump(
        mode="json"
    )
    value["artifact_descriptor_identity"] = d.identity
    return {
        "contract_version": "policy-owner-evidence-intake-v3",
        "kind": "observation",
        "key": d.acquisition_identity,
        "value": value,
    }


@pytest.mark.windows
def test_two_fresh_acquisitions_share_immutable_content_but_not_authentication(tmp_path):
    b, *_ = owner_case(tmp_path)
    raw, ref = host_bytes()
    now = datetime.now(timezone.utc)
    for index in range(2):
        at = now + timedelta(seconds=index)
        b.owner.now_utc = at.isoformat()
        d = descriptor(b, ref, "SYNTHETIC-distinct-acquisition-" + str(index), at)
        register = entry(b, d, raw)
        b.owner.validate_authentication_document("REGISTER_EVIDENCE", register)
        bounds = b.owner.authentication_validation_window("REGISTER_EVIDENCE", register)
        assert bounds["valid_until_utc"] == d.expires_utc
        b.apply("REGISTER_EVIDENCE", register, at=at)
        obs = observation(b, d, at)
        b.apply("REGISTER_EVIDENCE", obs, at=at)
        assert b.owner.observation(d.acquisition_identity).artifact == ref
        assert b.owner.read_artifact(ref) == raw
        with pytest.raises(ValueError):
            b.owner.validate_authentication_document("REGISTER_EVIDENCE", register)
    renewed = descriptor(b, ref, "SYNTHETIC-distinct-acquisition-0", now + timedelta(seconds=3))
    b.owner.now_utc = renewed.issued_utc
    with pytest.raises(ValueError, match="acquisition"):
        b.owner.validate_authentication_document("REGISTER_EVIDENCE", entry(b, renewed, raw))
    with pytest.raises(ValueError, match="acquisition"):
        b.owner.validate_authentication_document(
            "REGISTER_EVIDENCE", b.seed_contract_bytes(renewed, raw)
        )


@pytest.mark.windows
def test_v3_rejects_old_wrapper_missing_or_substituted_descriptor_and_old_policy(tmp_path):
    b, *_ = owner_case(tmp_path)
    raw, ref = host_bytes()
    at = datetime.now(timezone.utc)
    b.owner.now_utc = at.isoformat()
    d = descriptor(b, ref, "SYNTHETIC-new-host-acquisition", at)
    register = entry(b, d, raw)
    with pytest.raises(ValueError):
        evidence_intake_entries(
            {
                "contract_version": "policy-owner-evidence-intake-batch-v2",
                "entries": [register],
                "execution_authorized": False,
            }
        )
    obs = observation(b, d, at)
    with pytest.raises(ValueError):
        b.owner.validate_authentication_document("REGISTER_EVIDENCE", obs)
    b.apply("REGISTER_EVIDENCE", register, at=at)
    missing = {
        **obs,
        "value": {k: v for k, v in obs["value"].items() if k != "artifact_descriptor_identity"},
    }
    with pytest.raises(ValueError):
        b.owner.validate_authentication_document("REGISTER_EVIDENCE", missing)
    wrong = {
        **obs,
        "key": "SYNTHETIC-relabelled",
        "value": {**obs["value"], "observation_id": "SYNTHETIC-relabelled"},
    }
    with pytest.raises(ValueError):
        b.owner.validate_authentication_document("REGISTER_EVIDENCE", wrong)
    body = d.model_dump(mode="json", exclude={"identity"})
    body["approval_identity"] = b.old_approval.approval_identity
    body["acquisition_identity"] = "SYNTHETIC-old-policy"
    old = ArtifactDescriptor.seal(**body)
    with pytest.raises(ValueError):
        b.owner.validate_authentication_document("REGISTER_EVIDENCE", entry(b, old, raw))

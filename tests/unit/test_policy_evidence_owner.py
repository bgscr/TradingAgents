"""Synthetic TEST authority boundary; these records cannot authenticate natively."""

import base64
import json
import runpy
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from tradingagents.policy_evidence_acquisition import capture_roots
from tradingagents.policy_evidence_contract import ArtifactDescriptor
from tradingagents.policy_owner_contract import evidence_intake_entries
from tradingagents.policy_qualified_contract import BytePin


def make_case(tmp_path, change=None):
    factory = runpy.run_path(
        str(Path(__file__).parents[1] / "fixtures/policy_owner/local_owner_fixture.py")
    )["SyntheticNativeBoundary"]
    boundary = factory(tmp_path / "boundary")
    root = tmp_path / "DISPOSABLE"
    root.mkdir()
    (root / "sample").write_bytes(b"complete fixture capture")
    path = tmp_path / "capture.json"
    value = capture_roots(
        {
            "contract_version": "policy-evidence-root-list-v1",
            "fixture_only": True,
            "roots": [{"path": str(root), "security_descriptors_requested": True}],
        },
        path,
        phase="before",
        maximum_seconds=10,
    )
    now = datetime.now(timezone.utc)
    payload = {
        "contract_version": "policy-owner-artifact-descriptor-v2",
        "operation": "REGISTER_EVIDENCE",
        "instance": boundary.owner.anchor["instance"],
        "role_identity": boundary.role.identity,
        "approval_identity": boundary.approval.approval_identity,
        "candidate": boundary.approval.candidate,
        "lineage_plan_identity": boundary.approval.lineage_plan_identity,
        "acquisition_identity": value["capture"]["observation_identity"],
        "stage": "ISO02",
        "phase": "before",
        "artifact_schema": "crash-protection-historical-inventory:v1",
        "artifact_identity_field": "identity",
        "artifact": {"identity": value["identity"], "pin": BytePin.of(path.read_bytes())},
        "issued_utc": now.isoformat(),
        "expires_utc": (now + timedelta(minutes=10)).isoformat(),
    }
    if change:
        payload.update(change(boundary))
    descriptor = ArtifactDescriptor.seal(**payload)
    entry = boundary.seed_contract_bytes(descriptor, path.read_bytes())
    boundary.owner.now_utc = now.isoformat()
    return boundary, descriptor, entry


def test_compact_registration_admits_only_complete_bound_bytes(tmp_path):
    boundary, descriptor, entry = make_case(tmp_path)
    with pytest.raises(ValueError, match="not registered"):
        boundary.owner.read_artifact(descriptor.artifact)
    boundary.owner.validate_authentication_document("REGISTER_EVIDENCE", entry)
    window = boundary.owner.authentication_validation_window("REGISTER_EVIDENCE", entry)
    assert window["instance"] == boundary.owner.anchor["instance"]
    assert window["valid_from_utc"] == descriptor.issued_utc
    assert window["valid_until_utc"] == descriptor.expires_utc
    from hashlib import sha256

    from tradingagents.policy_qualified_contract import canonical_bytes

    assert window["document_sha256"] == sha256(canonical_bytes(entry)).hexdigest()
    boundary.apply("REGISTER_EVIDENCE", entry)
    assert BytePin.of(boundary.owner.read_artifact(descriptor.artifact)) == descriptor.artifact.pin
    with pytest.raises(ValueError, match="already exists|replayed"):
        boundary.owner.validate_authentication_document("REGISTER_EVIDENCE", entry)


@pytest.mark.parametrize(
    "change",
    [
        lambda b: {"instance": "test-20300101-002"},
        lambda b: {"role_identity": "policy-owner-project-role-proposal:v1:sha256:" + "0" * 64},
        lambda b: {"candidate": {"commit": "d" * 40, "tree": "b" * 40, "parent": "c" * 40}},
        lambda b: {"lineage_plan_identity": "historical-state-lineage-plan:v1:sha256:" + "0" * 64},
        lambda b: {"acquisition_identity": b.policy.scope.after.observation_id},
        lambda b: {"acquisition_identity": "DIFFERENT-new-observation"},
    ],
)
def test_context_mismatches_cannot_be_registered(tmp_path, change):
    boundary, descriptor, entry = make_case(tmp_path, change)
    with pytest.raises(ValueError):
        boundary.owner.validate_authentication_document("REGISTER_EVIDENCE", entry)
    with pytest.raises(ValueError, match="not registered"):
        boundary.owner.read_artifact(descriptor.artifact)


def test_expired_new_request_rejects_but_retained_record_derivation_uses_record_time(tmp_path):
    boundary, descriptor, entry = make_case(tmp_path)
    retained = boundary.record("REGISTER_EVIDENCE", entry)
    boundary.owner.now_utc = (datetime.now(timezone.utc) + timedelta(hours=2)).isoformat()
    with pytest.raises(ValueError, match="not currently valid"):
        boundary.owner.validate_authentication_document("REGISTER_EVIDENCE", entry)
    boundary.owner.records.append(
        retained
    )  # explicit synthetic native-record boundary, no native proof
    current = boundary.owner.now_utc
    boundary.owner.apply_authenticated_record(retained)
    assert boundary.owner.now_utc == current
    assert BytePin.of(boundary.owner.read_artifact(descriptor.artifact)) == descriptor.artifact.pin


def test_compact_intake_cannot_be_downgraded_to_v1(tmp_path):
    _boundary, _descriptor, entry = make_case(tmp_path)
    with pytest.raises(ValueError, match="downgrade"):
        evidence_intake_entries({**entry, "contract_version": "policy-owner-evidence-intake-v1"})
    with pytest.raises(ValueError, match="v2 batch"):
        evidence_intake_entries(
            {
                "contract_version": "policy-owner-evidence-intake-batch-v1",
                "entries": [entry],
                "execution_authorized": False,
            }
        )


def test_conflicting_setup_reference_rejects_before_authentication(tmp_path):
    boundary, descriptor, entry = make_case(tmp_path)
    blob = boundary.owner.root / "artifacts" / (descriptor.artifact.pin.sha256 + ".blob")
    alternate = json.dumps(json.loads(blob.read_bytes()), indent=2).encode("utf-8")
    old = boundary.owner.put(alternate, descriptor.artifact.identity)
    count = len(boundary.owner.records)
    with pytest.raises(ValueError, match="rebind"):
        boundary.owner.validate_authentication_document("REGISTER_EVIDENCE", entry)
    assert len(boundary.owner.records) == count
    assert BytePin.of(boundary.owner.read_artifact(old)) == old.pin


def test_cross_version_artifact_collision_in_one_batch_rejects_before_authentication(tmp_path):
    boundary, descriptor, entry = make_case(tmp_path)
    blob = boundary.owner.root / "artifacts" / (descriptor.artifact.pin.sha256 + ".blob")
    raw = blob.read_bytes()
    inline = {
        "contract_version": "policy-owner-evidence-intake-v1",
        "kind": "artifact",
        "key": descriptor.artifact.identity,
        "value": {
            "reference": descriptor.artifact.model_dump(mode="json"),
            "bytes_base64": base64.b64encode(raw).decode("ascii"),
        },
    }
    count = len(boundary.owner.records)
    with pytest.raises(ValueError, match="duplicate"):
        boundary.owner.validate_authentication_document(
            "REGISTER_EVIDENCE",
            {
                "contract_version": "policy-owner-evidence-intake-batch-v2",
                "entries": [inline, entry],
                "execution_authorized": False,
            },
        )
    assert len(boundary.owner.records) == count
    with pytest.raises(ValueError, match="not registered"):
        boundary.owner.read_artifact(descriptor.artifact)


def test_opaque_bytes_cannot_be_reinterpreted_as_current_source(tmp_path):
    """A mathematically valid TEST proof is still the wrong authenticated schema."""
    from hashlib import sha1

    from tradingagents.policy_evidence_acquisition import ProductionInventory
    from tradingagents.windows_sandbox_runner import SourceManifestEntry, _tree_hash_from_entries

    data = b'"""SYNTHETIC PROOF VECTOR, NOT PHYSICALLY CAPTURED OR QUALIFIED."""\n'
    member = SourceManifestEntry(
        relative_path="tradingagents/__init__.py",
        git_mode="100644",
        git_object_id=sha1(b"blob " + str(len(data)).encode() + b"\0" + data).hexdigest(),
        byte_length=len(data),
        sha256=BytePin.of(data).sha256,
    )
    tree = _tree_hash_from_entries((member,), object_format="sha1")
    commit_object = ("tree " + tree + "\nparent " + "c" * 40 + "\n\nSYNTHETIC fixture\n").encode()
    candidate = {
        "commit": sha1(
            b"commit " + str(len(commit_object)).encode() + b"\0" + commit_object
        ).hexdigest(),
        "tree": tree,
        "parent": "c" * 40,
    }
    now = datetime.now(timezone.utc)
    inventory = ProductionInventory.seal(
        contract_version="production-python-modules-v1",
        candidate=candidate,
        repository_root=str(tmp_path / "NOT-PHYSICALLY-CAPTURED"),
        git_tool={
            "path": r"C:\SYNTHETIC\git.exe",
            "pin": BytePin.of(b"SYNTHETIC").model_dump(mode="json"),
            "version": "git version SYNTHETIC",
        },
        commit_object_base64=base64.b64encode(commit_object).decode(),
        fixture_only=True,
        entries=[member.model_dump(mode="json")],
        physical_entries=[
            {
                "relative_path": member.relative_path,
                "pin": BytePin.of(data).model_dump(mode="json"),
                "checkout_form": "IDENTICAL",
            }
        ],
        production_modules=[member.relative_path],
        inclusion_rule="complete-reviewed-git-tree-and-physical-production-python-v1",
        started_utc=now.isoformat(),
        finished_utc=now.isoformat(),
    )
    factory = runpy.run_path(
        str(Path(__file__).parents[1] / "fixtures/policy_owner/local_owner_fixture.py")
    )["SyntheticNativeBoundary"]
    boundary = factory(tmp_path / "boundary", candidate=candidate)
    raw = inventory.to_bytes()
    pin = BytePin.of(raw)
    descriptor = ArtifactDescriptor.seal(
        contract_version="policy-owner-artifact-descriptor-v2",
        operation="REGISTER_EVIDENCE",
        instance=boundary.owner.anchor["instance"],
        role_identity=boundary.role.identity,
        approval_identity=boundary.approval.approval_identity,
        candidate=candidate,
        lineage_plan_identity=boundary.approval.lineage_plan_identity,
        acquisition_identity="SYNTHETIC-OPAQUE",
        stage="CURRENT_SOURCE",
        phase="evidence",
        artifact_schema="policy-evidence-bytes:v1",
        artifact_identity_field="byte_digest",
        artifact={"identity": "policy-evidence-bytes:v1:sha256:" + pin.sha256, "pin": pin},
        issued_utc=now.isoformat(),
        expires_utc=(now + timedelta(hours=1)).isoformat(),
    )
    boundary.apply("REGISTER_EVIDENCE", boundary.seed_contract_bytes(descriptor, raw))
    count = len(boundary.owner.records)
    with pytest.raises(ValueError, match="source schema"):
        boundary.owner.validate_authentication_document(
            "REGISTER_EVIDENCE",
            {
                "contract_version": "policy-owner-evidence-intake-v2",
                "kind": "current-source",
                "key": "current",
                "value": {
                    "candidate": candidate,
                    "lineage_plan_identity": boundary.approval.lineage_plan_identity,
                    "production_inventory_identity": descriptor.artifact.identity,
                    "inventory_reference": descriptor.artifact.model_dump(mode="json"),
                },
            },
        )
    assert len(boundary.owner.records) == count

"""Compact descriptors bind complete evidence; parsing never supplies authority."""

from datetime import datetime, timedelta, timezone

import pytest

from tradingagents.policy_evidence_contract import ArtifactDescriptor, validate_artifact
from tradingagents.policy_qualified_contract import BytePin, canonical_bytes, content_identity


def raw_inventory():
    rows = [
        {
            "path": ".",
            "kind": "directory",
            "attributes": 268435472,
            "mtime_ns": 1,
            "ctime_ns": 1,
            "descriptor_sha256": "f" * 64,
        }
    ]
    root = {
        "path": "D:\\DISPOSABLE-ONLY",
        "entries": rows,
        "file_count": 0,
        "unreadable_count": 0,
        "security_descriptors_requested": True,
        "inventory_identity": content_identity(
            "crash-protection-historical-root-inventory:v1", rows
        ),
    }
    body = {
        "phase": "before",
        "roots": [root],
        "elapsed_seconds": 0.25,
        "file_count": 0,
        "unreadable_count": 0,
        "no_acl_write_or_recovery": True,
    }
    body["identity"] = content_identity("crash-protection-historical-inventory:v1", body)
    return canonical_bytes(body) + b"\n", body["identity"]


def descriptor(raw, identity, **changes):
    now = datetime(2030, 1, 1, tzinfo=timezone.utc)
    values = {
        "contract_version": "policy-owner-artifact-descriptor-v2",
        "operation": "REGISTER_EVIDENCE",
        "instance": "test-20300101-001",
        "role_identity": "policy-owner-project-role-proposal:v1:sha256:" + "1" * 64,
        "approval_identity": "historical-state-operator-approval:v1:sha256:" + "2" * 64,
        "candidate": {"commit": "a" * 40, "tree": "b" * 40, "parent": "c" * 40},
        "lineage_plan_identity": "historical-state-lineage-plan:v1:sha256:" + "3" * 64,
        "acquisition_identity": "fixture-acquisition-001",
        "stage": "ISO02",
        "phase": "before",
        "artifact_schema": "crash-protection-historical-inventory:v1",
        "artifact_identity_field": "identity",
        "artifact": {"identity": identity, "pin": BytePin.of(raw).model_dump(mode="json")},
        "issued_utc": now.isoformat(),
        "expires_utc": (now + timedelta(hours=1)).isoformat(),
    }
    values.update(changes)
    return ArtifactDescriptor.seal(**values)


def test_complete_raw_artifact_is_bound_by_compact_descriptor(tmp_path):
    raw, identity = raw_inventory()
    path = tmp_path / "observation.json"
    path.write_bytes(raw)
    bound = descriptor(raw, identity)
    result = validate_artifact(path, bound)
    assert result["roots"][0]["entries"][0]["attributes"] == 0x10000010
    assert b"bytes_base64" not in bound.to_bytes()
    assert len(bound.to_bytes()) < 2048


def test_descriptor_does_not_accept_changed_payload(tmp_path):
    raw, identity = raw_inventory()
    path = tmp_path / "observation.json"
    path.write_bytes(raw.replace(b"268435472", b"000000016"))
    with pytest.raises(ValueError, match="pin|digest"):
        validate_artifact(path, descriptor(raw, identity))


def test_unsupported_schema_and_wrong_identity_field_reject(tmp_path):
    raw, identity = raw_inventory()
    path = tmp_path / "observation.json"
    path.write_bytes(raw)
    with pytest.raises(ValueError, match="schema"):
        descriptor(raw, identity, artifact_schema="unimplemented-format:v9")
    with pytest.raises(ValueError, match="identity field"):
        validate_artifact(path, descriptor(raw, identity, artifact_identity_field="wrong_field"))


def test_binary_log_evidence_keeps_every_byte_under_an_explicit_schema(tmp_path):
    raw = b"complete log\r\n\x00\xff\x01\r\n"
    identity = "policy-evidence-bytes:v1:sha256:" + BytePin.of(raw).sha256
    path = tmp_path / "log.bin"
    path.write_bytes(raw)
    bound = descriptor(
        raw,
        identity,
        artifact_schema="policy-evidence-bytes:v1",
        artifact_identity_field="byte_digest",
    )
    assert validate_artifact(path, bound) == {"opaque_complete_bytes": True}
    with pytest.raises(ValueError, match="schema"):
        validate_artifact(
            path,
            descriptor(
                raw,
                identity,
                artifact_schema="policy-evidence-bytes:v1",
                artifact_identity_field="identity",
            ),
        )


def test_descriptor_has_finite_byte_and_validity_bounds():
    raw, identity = raw_inventory()
    with pytest.raises(ValueError, match="length"):
        descriptor(
            raw,
            identity,
            artifact={
                "identity": identity,
                "pin": {"byte_length": 256 * 1024**2 + 1, "sha256": "a" * 64},
            },
        )
    with pytest.raises(ValueError, match="lifetime"):
        descriptor(raw, identity, expires_utc="2030-01-01T01:00:01+00:00")


@pytest.mark.parametrize("kind", ["unreadable_object", "reparse", "missing", "unknown"])
def test_complete_flag_cannot_override_invalid_raw_rows(tmp_path, kind):
    from tradingagents.policy_qualified_contract import strict_json

    raw, _identity = raw_inventory()
    body = strict_json(raw)
    root = body["roots"][0]
    root["entries"][0]["kind"] = kind
    root["unreadable_count"] = 1 if kind.startswith("unreadable") else 0
    root["inventory_identity"] = content_identity(
        "crash-protection-historical-root-inventory:v1", root["entries"]
    )
    body["capture"] = {"complete": True, "errors": []}
    body.pop("identity")
    body["identity"] = content_identity("crash-protection-historical-inventory:v1", body)
    changed = canonical_bytes(body)
    path = tmp_path / "contradictory.json"
    path.write_bytes(changed)
    with pytest.raises(ValueError, match="incomplete|unsupported|not admissible"):
        validate_artifact(path, descriptor(changed, body["identity"]))


@pytest.mark.parametrize("missing", ["attributes", "mtime_ns", "ctime_ns", "descriptor_sha256"])
def test_requested_raw_security_fields_cannot_be_omitted(tmp_path, missing):
    from tradingagents.policy_qualified_contract import strict_json

    raw, _identity = raw_inventory()
    body = strict_json(raw)
    root = body["roots"][0]
    root["entries"][0].pop(missing)
    root["inventory_identity"] = content_identity(
        "crash-protection-historical-root-inventory:v1", root["entries"]
    )
    body.pop("identity")
    body["identity"] = content_identity("crash-protection-historical-inventory:v1", body)
    changed = canonical_bytes(body)
    path = tmp_path / "missing-fields.json"
    path.write_bytes(changed)
    with pytest.raises(ValueError, match="coverage"):
        validate_artifact(path, descriptor(changed, body["identity"]))

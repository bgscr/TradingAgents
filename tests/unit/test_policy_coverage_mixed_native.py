"""Small ordinary native fixtures; no links, ACL mutation, service or authority."""

import hashlib
import os

import pytest

from tradingagents.policy_coverage_contract import (
    RootTypedCoverageMeasurement,
    artifact_ref,
    build_scope,
    compare_current_inventories,
    compare_known_fields,
)
from tradingagents.policy_qualified_contract import Candidate, canonical_bytes, content_identity

pytestmark = pytest.mark.skipif(os.name != "nt", reason="native Windows ordinary objects")


def ordinary_context(root, kind, content=b""):
    row = {"path": ".", "kind": kind}
    if kind == "file":
        row.update(byte_length=len(content), sha256=hashlib.sha256(content).hexdigest())
    original = {}
    for phase in ("before", "after"):
        body = {
            "phase": phase,
            "roots": [
                {
                    "path": str(root),
                    "entries": [row],
                    "file_count": int(kind == "file"),
                    "unreadable_count": 0,
                    "security_descriptors_requested": False,
                    "inventory_identity": content_identity(
                        "crash-protection-historical-root-inventory:v1", [row]
                    ),
                }
            ],
            "file_count": int(kind == "file"),
            "unreadable_count": 0,
            "elapsed_seconds": 0.0,
            "no_acl_write_or_recovery": True,
        }
        body["identity"] = content_identity("crash-protection-historical-inventory:v1", body)
        original[phase] = canonical_bytes(body) + b"\n"
    scope = build_scope(original["before"], original["after"], fixture_only=True, mixed_roots=True)
    measurement = RootTypedCoverageMeasurement.seal(
        contract_version="policy-coverage-measurement-v3",
        root_kind_policy="EXACT_SAVED_ROOT_KINDS",
        scope=artifact_ref(scope),
        candidate=Candidate(commit="1" * 40, tree="2" * 40, parent="3" * 40),
        supported_reparse_tags=(0xA0000003,),
        security_required=True,
        maximum_entries=10,
        maximum_read_bytes=1024**2,
        maximum_seconds=30,
        maximum_depth=8,
        maximum_reparse_bytes=16384,
        maximum_indirect_link_depth=0,
        target_coverage="SEPARATE_ORDINARY_ROOT_WALK",
        known_fields_required=True,
    )
    return original, scope, measurement


@pytest.mark.parametrize("size", [0, 16529])
def test_actual_ordinary_file_root_never_enumerates_or_appends_dot(tmp_path, monkeypatch, size):
    from tradingagents import policy_coverage_native as native

    root = tmp_path / "ordinary-root-without-extension"
    content = b"x" * size
    root.write_bytes(content)
    original, scope, measurement = ordinary_context(root, "file", content)
    calls = []
    available = native._available_local

    def observed(path):
        calls.append(str(path))
        return available(path)

    def no_enumeration(path):
        raise AssertionError("ordinary file capture must not enumerate a directory")

    with monkeypatch.context() as context:
        context.setattr(native, "_available_local", observed)
        context.setattr(native.os, "scandir", no_enumeration)
        before = native.capture_coverage(
            scope,
            measurement,
            tmp_path / "before.json",
            instance="test-20990101-999",
            phase="before",
            fixture_only=True,
        )
        after = native.capture_coverage(
            scope,
            measurement,
            tmp_path / "after.json",
            instance="test-20990101-999",
            phase="after",
            fixture_only=True,
        )
    assert before["capture"]["complete"] and after["capture"]["complete"]
    assert before["capture"]["read_bytes"] == size
    assert all(not path.endswith("\\.") for path in calls)
    assert len(before["roots"][0]["entries"]) == 1
    assert before["roots"][0]["entries"][0]["attributes"] == root.lstat().st_file_attributes
    assert (
        compare_known_fields(
            scope, original["before"], original["after"], before, measurement
        ).known_recorded_fields
        == "MATCH"
    )
    assert compare_current_inventories(before, after, scope, measurement)["equal"]


@pytest.mark.parametrize("expected", ["file", "directory"])
def test_wrong_root_type_fails_before_content_read_or_directory_enumeration(
    tmp_path, monkeypatch, expected
):
    from tradingagents import policy_coverage_native as native

    root = tmp_path / "root"
    if expected == "file":
        root.mkdir()
    else:
        root.write_bytes(b"unexpected ordinary file")
    _, scope, measurement = ordinary_context(root, expected)

    def forbidden(*args):
        raise AssertionError("wrong root type reached content access")

    with monkeypatch.context() as context:
        context.setattr(native.os, "scandir", forbidden)
        context.setattr(native, "_chunks", forbidden)
        value = native.capture_coverage(
            scope,
            measurement,
            tmp_path / "rejected.json",
            instance="test-20990101-999",
            phase="before",
            fixture_only=True,
        )
    assert value["capture"]["complete"] is False
    assert value["capture"]["read_bytes"] == 0
    assert any(
        "actual ordinary root type differs from trusted scope" in str(error)
        for error in value["capture"]["errors"]
    )

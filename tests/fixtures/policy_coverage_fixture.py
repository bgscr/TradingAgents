"""Conspicuously synthetic evidence; no native authentication or real observation."""

import base64
import copy
import struct

from tradingagents.policy_qualified_contract import Candidate, canonical_bytes, content_identity


def junction_buffer(target):
    substitute = ("\\??\\" + target).encode("utf-16-le")
    display = target.encode("utf-16-le")
    names = substitute + b"\0\0" + display + b"\0\0"
    body = struct.pack("<HHHH", 0, len(substitute), len(substitute) + 2, len(display)) + names
    return struct.pack("<IHH", 0xA0000003, len(body), 0) + body


def seal_current(value):
    value = copy.deepcopy(value)
    value.pop("identity", None)
    for r in value["roots"]:
        r["inventory_identity"] = content_identity("policy-coverage-root:v2", r["entries"])
        r["file_count"] = sum(x["kind"] == "file" for x in r["entries"])
    capture = value["capture"]
    capture.pop("observation_identity", None)
    capture["observed_entries"] = sum(len(r["entries"]) for r in value["roots"])
    capture["logical_bytes"] = sum(
        x.get("byte_length", 0) for r in value["roots"] for x in r["entries"]
    )
    generated = capture["origin"] == "GENERATED_FIXTURE_NOT_AN_OBSERVATION"
    capture["read_bytes"] = 0 if generated else capture["logical_bytes"]
    capture["observation_identity"] = content_identity(
        "policy-coverage-fixture-acquisition:v2" if generated else "policy-coverage-observation:v2",
        capture,
    )
    value["identity"] = content_identity("policy-coverage-inventory:v2", value)
    return value


def make_coverage_fixture():
    from tradingagents.policy_coverage_contract import (
        CoverageMeasurement,
        artifact_ref,
        build_scope,
    )

    root = "D:\\FIXTURE_ONLY\\root"
    old_rows = [
        {"path": ".", "kind": "directory"},
        {"path": "gap", "kind": "directory"},
        {"path": "gap", "kind": "unreadable_directory", "attributes": 16, "mtime_ns": 10},
        {"path": "known.txt", "kind": "file", "byte_length": 3, "sha256": "a" * 64},
        {"path": "link", "kind": "reparse", "size": 0, "mtime_ns": 20},
        {"path": "target", "kind": "directory"},
    ]
    values = {}
    for phase in ("before", "after"):
        r = {
            "path": root,
            "entries": old_rows,
            "file_count": 1,
            "unreadable_count": 1,
            "security_descriptors_requested": False,
            "inventory_identity": content_identity(
                "crash-protection-historical-root-inventory:v1", old_rows
            ),
        }
        value = {
            "phase": phase,
            "roots": [r],
            "file_count": 1,
            "unreadable_count": 1,
            "no_acl_write_or_recovery": True,
            "elapsed_seconds": 1.0,
        }
        value["identity"] = content_identity("crash-protection-historical-inventory:v1", value)
        values[phase] = canonical_bytes(value) + b"\n"
    scope = build_scope(values["before"], values["after"], fixture_only=True)
    candidate = Candidate(commit="1" * 40, tree="2" * 40, parent="3" * 40)
    measurement = CoverageMeasurement.seal(
        contract_version="policy-coverage-measurement-v2",
        scope=artifact_ref(scope),
        candidate=candidate,
        supported_reparse_tags=(0xA0000003,),
        security_required=True,
        maximum_entries=2_000_000,
        maximum_read_bytes=64 * 1024**3,
        maximum_seconds=600,
        maximum_depth=128,
        maximum_reparse_bytes=16384,
        maximum_indirect_link_depth=0,
        target_coverage="SEPARATE_ORDINARY_ROOT_WALK",
        known_fields_required=True,
    )
    rows = []
    for i, (path, kind) in enumerate(
        [
            (".", "directory"),
            ("gap", "directory"),
            ("gap/newly-observed.txt", "file"),
            ("known.txt", "file"),
            ("link", "reparse"),
            ("target", "directory"),
        ]
    ):
        row = {
            "path": path,
            "kind": kind,
            "attributes": 16 if kind == "directory" else (1040 if kind == "reparse" else 32),
            "mtime_ns": 10 if path == "gap" else (20 if path == "link" else 1),
            "ctime_ns": 1,
            "descriptor_sha256": "b" * 64,
            "object_identity": {"volume_serial": "12345678", "file_index": f"{i + 1:016x}"},
        }
        if kind == "file":
            row.update(byte_length=3, sha256="a" * 64)
        if kind == "reparse":
            row.update(
                size=0,
                tag=0xA0000003,
                reparse_data_base64=base64.b64encode(junction_buffer(root + "\\target")).decode(
                    "ascii"
                ),
                target_relative_path="target",
                target_object_identity={
                    "volume_serial": "12345678",
                    "file_index": "0000000000000006",
                },
                target_root_identity={
                    "volume_serial": "12345678",
                    "file_index": "0000000000000001",
                },
            )
        rows.append(row)
    r = {
        "path": root,
        "entries": rows,
        "file_count": 2,
        "unreadable_count": 0,
        "security_descriptors_requested": True,
        "inventory_identity": "",
    }
    value = {
        "contract_version": "policy-coverage-inventory-v2",
        "scope": artifact_ref(scope).model_dump(mode="json"),
        "measurement": artifact_ref(measurement).model_dump(mode="json"),
        "candidate": candidate.model_dump(mode="json"),
        "instance": "test-20990101-999",
        "phase": "before",
        "roots": [r],
        "fixture_only": True,
        "capture": {
            "contract_version": "policy-coverage-acquisition-v2",
            "origin": "GENERATED_FIXTURE_NOT_AN_OBSERVATION",
            "nonce": "a" * 32,
            "started_utc": "2099-01-01T00:00:00+00:00",
            "finished_utc": "2099-01-01T00:00:01+00:00",
            "collector_pin": {"byte_length": 1, "sha256": "c" * 64},
            "runtime_pin": {"byte_length": 1, "sha256": "d" * 64},
            "method": "generated-coverage-schema-vector-v2",
            "complete": True,
            "errors": [],
            "observed_entries": 0,
            "read_bytes": 0,
        },
    }
    values.update(scope=scope, measurement=measurement, baseline=seal_current(value))
    return values

"""Labelled TEST data, never a measurement or an authentication implementation."""

from __future__ import annotations

import base64
import copy
import hashlib
import struct
import sys
import uuid
from pathlib import Path

from tradingagents.policy_coverage_contract import artifact_ref, validate_current_inventory
from tradingagents.policy_qualified_contract import BytePin, content_identity, strict_json


def historical_rows(*, links=5322):
    """Seven distinct error markers/companions, and the full declared link shape."""
    if not 1 <= links <= 5322:
        raise ValueError("bounded generated TEST link count required")
    rows = [
        {"path": "gaps", "kind": "directory"},
        {"path": "links", "kind": "directory"},
        {"path": "targets", "kind": "directory"},
    ]
    for index in range(7):
        rows.extend(
            [
                {"path": f"gaps/gap-{index:02}", "kind": "directory"},
                {
                    "path": f"gaps/gap-{index:02}",
                    "kind": "unreadable_directory",
                    "attributes": 16,
                    "mtime_ns": 100 + index,
                },
            ]
        )
    for index in range(links):
        rows.extend(
            [
                {
                    "path": f"links/link-{index:05}",
                    "kind": "reparse",
                    "size": 0,
                    "mtime_ns": 200 + index,
                },
                {"path": f"targets/target-{index:05}", "kind": "directory"},
            ]
        )
    return rows


def seal_generated(value):
    """Keep generated logical bytes distinct from actual read bytes/observations."""
    value = copy.deepcopy(value)
    for root in value["roots"]:
        root["entries"].sort(key=lambda row: row["path"].casefold())
        root["inventory_identity"] = content_identity("policy-coverage-root:v2", root["entries"])
    capture = value["capture"]
    capture.pop("observation_identity", None)
    capture["observed_entries"] = sum(len(root["entries"]) for root in value["roots"])
    capture["logical_bytes"] = sum(
        row.get("byte_length", 0) for root in value["roots"] for row in root["entries"]
    )
    capture["observation_identity"] = content_identity(
        "policy-coverage-fixture-acquisition:v2", capture
    )
    value.pop("identity", None)
    value["identity"] = content_identity("policy-coverage-inventory:v2", value)
    return value


def generated_baseline(scope, measurement, original_after, *, instance, clock):
    if not scope.fixture_only or not instance.startswith("test-"):
        raise ValueError("generated vectors require explicit TEST scope and instance")
    original = strict_json(original_after)
    scope.after.pin.check(original_after)
    roots = []
    for ri, old in enumerate(original["roots"]):
        rows = [
            copy.deepcopy(row) for row in old["entries"] if row["kind"] != "unreadable_directory"
        ]
        gaps = {item.row["path"]: item.row for item in scope.gaps if item.root_index == ri}
        for row in rows:
            known = gaps.get(row["path"], {})
            for key in ("attributes", "mtime_ns", "ctime_ns", "descriptor_sha256"):
                if key in known:
                    if key in row and row[key] != known[key]:
                        raise ValueError("conflicting generated marker fields")
                    row[key] = known[key]
            row.setdefault(
                "attributes",
                1040 if row["kind"] == "reparse" else 16 if row["kind"] == "directory" else 32,
            )
            row.setdefault("ctime_ns", 1)
            row.setdefault("mtime_ns", 1)
            row.setdefault(
                "descriptor_sha256",
                hashlib.sha256(("TEST descriptor " + row["path"]).encode()).hexdigest(),
            )
        for name in gaps:
            rows.append(
                {
                    "path": name + "/newly-observed.txt",
                    "kind": "file",
                    "attributes": 32,
                    "ctime_ns": 1,
                    "mtime_ns": 1,
                    "descriptor_sha256": "a" * 64,
                    "byte_length": 3,
                    "sha256": hashlib.sha256(b"NEW").hexdigest(),
                }
            )
        rows.sort(key=lambda row: row["path"].casefold())
        for index, row in enumerate(rows):
            row["object_identity"] = {
                "volume_serial": f"{ri + 1:08x}",
                "file_index": f"{index + 1:016x}",
            }
        by_path = {row["path"]: row for row in rows}
        for row in rows:
            if row["kind"] != "reparse":
                continue
            target = "targets/target-" + row["path"].rsplit("-", 1)[1]
            if target not in by_path:
                raise ValueError("generated link has no declared ordinary target")
            path = old["path"] + "\\" + target.replace("/", "\\")
            substitute = ("\\??\\" + path).encode("utf-16-le")
            printed = path.encode("utf-16-le")
            data = (
                struct.pack("<HHHH", 0, len(substitute), len(substitute) + 2, len(printed))
                + substitute
                + b"\0\0"
                + printed
                + b"\0\0"
            )
            raw = struct.pack("<IHH", 0xA0000003, len(data), 0) + data
            row.update(
                tag=0xA0000003,
                reparse_data_base64=base64.b64encode(raw).decode(),
                target_relative_path=target,
                target_object_identity=by_path[target]["object_identity"],
                target_root_identity=by_path["."]["object_identity"],
            )
        roots.append(
            {
                "path": old["path"],
                "entries": rows,
                "file_count": sum(row["kind"] == "file" for row in rows),
                "unreadable_count": 0,
                "security_descriptors_requested": True,
                "inventory_identity": "",
            }
        )
    capture = {
        "contract_version": "policy-coverage-acquisition-v2",
        "origin": "GENERATED_FIXTURE_NOT_AN_OBSERVATION",
        "nonce": uuid.uuid4().hex,
        "started_utc": clock,
        "finished_utc": clock,
        "collector_pin": BytePin.of(Path(__file__).read_bytes()).model_dump(mode="json"),
        "runtime_pin": BytePin.of(Path(sys.executable).read_bytes()).model_dump(mode="json"),
        "method": "generated-coverage-schema-vector-v2",
        "complete": True,
        "errors": [],
        "observed_entries": 0,
        "read_bytes": 0,
        "logical_bytes": 0,
    }
    value = seal_generated(
        {
            "contract_version": "policy-coverage-inventory-v2",
            "scope": artifact_ref(scope).model_dump(mode="json"),
            "measurement": artifact_ref(measurement).model_dump(mode="json"),
            "candidate": measurement.candidate.model_dump(mode="json"),
            "instance": instance,
            "phase": "before",
            "roots": roots,
            "fixture_only": True,
            "capture": capture,
        }
    )
    validate_current_inventory(value, scope, measurement)
    return value

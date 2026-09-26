"""Versioned saved-coverage scope and prospective measurement comparisons.

These are evidence predicates, never authentication or permission to acquire.
Historical UNKNOWN is explicit and is never promoted to historical equality.
"""

from __future__ import annotations

import base64
import re
import struct
from pathlib import PureWindowsPath
from typing import Literal

from pydantic import Field, model_validator
from typing_extensions import Self

from tradingagents.policy_qualified_contract import (
    BytePin,
    Candidate,
    Closed,
    EvidenceRef,
    Identity,
    Sealed,
    Text,
    canonical_bytes,
    content_identity,
    strict_json,
    utc,
)

HISTORICAL_NAMESPACE = "crash-protection-historical-inventory:v1"
CURRENT_NAMESPACE = "policy-coverage-inventory:v2"
MAX_INVENTORY_BYTES = 256 * 1024**2
MOUNT_POINT_TAG = 0xA0000003


class SavedCoverageRow(Closed):
    root: Text
    root_index: int = Field(ge=0)
    row_index: int = Field(ge=0)
    row: dict
    companion_index: int | None = Field(ge=0)
    companion: dict | None


class HistoricalCoverageScope(Sealed):
    namespace = "historical-coverage-scope:v2"
    contract_version: Literal["historical-coverage-scope-v2"]
    before: EvidenceRef
    after: EvidenceRef
    roots: tuple[str, ...] = Field(min_length=1, max_length=10000)
    gaps: tuple[SavedCoverageRow, ...]
    links: tuple[SavedCoverageRow, ...]
    fixture_only: bool
    historical_coverage: Literal["UNKNOWN"]
    historical_object_continuity: Literal["NOT_ESTABLISHED"]
    hidden_contents: Literal["UNKNOWN_NOT_RETROACTIVELY_COMPLETED"]
    acquisition_only_fields: tuple[str, ...]
    historically_unavailable_fields: tuple[str, ...]
    identity: Identity

    @model_validator(mode="after")
    def _closed_scope(self) -> Self:
        if len({x.casefold() for x in self.roots}) != len(self.roots):
            raise ValueError("duplicate or aliased coverage root")
        if self.before.identity == self.after.identity or self.before.pin == self.after.pin:
            raise ValueError("distinct original before/after artifacts required")
        for values, kind in ((self.gaps, "unreadable_directory"), (self.links, "reparse")):
            keys = [(x.root_index, x.row_index) for x in values]
            if keys != sorted(set(keys)):
                raise ValueError("coverage manifest is duplicated or unordered")
            for item in values:
                if (
                    item.root_index >= len(self.roots)
                    or self.roots[item.root_index] != item.root
                    or item.row.get("kind") != kind
                ):
                    raise ValueError("coverage row root/kind binding mismatch")
                if kind == "unreadable_directory" and (
                    item.companion_index is None
                    or item.companion_index == item.row_index
                    or item.companion is None
                    or item.companion.get("kind") != "directory"
                    or item.companion.get("path") != item.row.get("path")
                ):
                    raise ValueError("failure marker and directory companion must remain distinct")
                if kind == "reparse" and (
                    item.companion is not None or item.companion_index is not None
                ):
                    raise ValueError("reparse row cannot invent a historical target companion")
        return self


def read_historical(raw: bytes) -> tuple[dict, EvidenceRef]:
    if not 0 < len(raw) <= MAX_INVENTORY_BYTES:
        raise ValueError("historical artifact exceeds bounded evidence capacity")
    value = strict_json(raw)
    if not isinstance(value, dict) or value.get("identity") != content_identity(
        HISTORICAL_NAMESPACE, {k: v for k, v in value.items() if k != "identity"}
    ):
        raise ValueError("historical canonical identity mismatch")
    from tradingagents.policy_qualified_results import PolicyQualifiedVerifier

    PolicyQualifiedVerifier._roots(value)
    if value.get("no_acl_write_or_recovery") is not True:
        raise ValueError("historical no-repair assertion missing")
    return value, EvidenceRef(identity=value["identity"], pin=BytePin.of(raw))


def build_scope(
    before_bytes: bytes, after_bytes: bytes, *, fixture_only: bool
) -> HistoricalCoverageScope:
    """Derive ALL saved exceptional records, with no caller-selected subset."""
    before, before_ref = read_historical(before_bytes)
    after, after_ref = read_historical(after_bytes)
    if before.get("phase") != "before" or after.get("phase") != "after":
        raise ValueError("original before/after acquisition phases required")
    if [x["path"] for x in before["roots"]] != [x["path"] for x in after["roots"]]:
        raise ValueError("original root scopes differ")
    gaps, links = [], []
    for ri, root in enumerate(after["roots"]):
        rows = root["entries"]
        indexed = {(r["path"], r["kind"]): (i, r) for i, r in enumerate(rows)}
        old_indexed = {(r["path"], r["kind"]): r for r in before["roots"][ri]["entries"]}
        if root.get("file_count") != sum(row["kind"] == "file" for row in rows) or root.get(
            "unreadable_count"
        ) != sum(row["kind"].startswith("unreadable") for row in rows):
            raise ValueError("historical coverage counters disagree with complete rows")
        for ei, row in enumerate(rows):
            if row["kind"] not in {"file", "directory", "unreadable_directory", "reparse"}:
                raise ValueError("unsupported historical coverage failure type")
            if row["kind"] not in {"unreadable_directory", "reparse"}:
                continue
            item = {
                "root": root["path"],
                "root_index": ri,
                "row_index": ei,
                "row": row,
                "companion_index": None,
                "companion": None,
            }
            if row["kind"] == "unreadable_directory":
                companion = indexed.get((row["path"], "directory"))
                if companion is None:
                    raise ValueError("historical failure lacks its unique directory companion")
                item.update(companion_index=companion[0], companion=companion[1])
                gaps.append(item)
            else:
                links.append(item)
            if old_indexed.get((row["path"], row["kind"])) != row:
                raise ValueError("coverage marker/link changed within original pair")
    return HistoricalCoverageScope.seal(
        contract_version="historical-coverage-scope-v2",
        before=before_ref,
        after=after_ref,
        roots=tuple(root["path"] for root in after["roots"]),
        gaps=tuple(gaps),
        links=tuple(links),
        fixture_only=fixture_only,
        historical_coverage="UNKNOWN",
        historical_object_continuity="NOT_ESTABLISHED",
        hidden_contents="UNKNOWN_NOT_RETROACTIVELY_COMPLETED",
        acquisition_only_fields=(
            "failure_kind",
            "security_read_error",
            "unreadable_count",
            "file_count",
            "security_descriptors_requested",
            "inventory_identity",
            "acquisition_provenance",
        ),
        historically_unavailable_fields=(
            "unobserved_descendants",
            "unrecorded_security_descriptors",
            "object_volume_identity",
            "reparse_tag_data_target",
            "unrecorded_current_metadata",
        ),
    )


def validate_scope(
    scope: object, before_bytes: bytes, after_bytes: bytes
) -> HistoricalCoverageScope:
    scope = HistoricalCoverageScope.read(scope)
    if build_scope(before_bytes, after_bytes, fixture_only=scope.fixture_only) != scope:
        raise ValueError("coverage scope does not preserve the complete exact original record set")
    return scope


def artifact_ref(value) -> EvidenceRef:
    raw = value.to_bytes() if isinstance(value, Sealed) else canonical_bytes(value) + b"\n"
    identity = value.identity if isinstance(value, Sealed) else value["identity"]
    return EvidenceRef(identity=identity, pin=BytePin.of(raw))


class CoverageMeasurement(Sealed):
    namespace = "policy-coverage-measurement:v2"
    contract_version: Literal["policy-coverage-measurement-v2"]
    scope: EvidenceRef
    candidate: Candidate
    supported_reparse_tags: tuple[Literal[0xA0000003], ...] = Field(min_length=1, max_length=1)
    security_required: Literal[True]
    maximum_entries: int = Field(ge=1, le=2_000_000)
    maximum_read_bytes: int = Field(ge=1, le=64 * 1024**3)
    maximum_seconds: int = Field(ge=1, le=1800)
    maximum_depth: int = Field(ge=1, le=128)
    maximum_reparse_bytes: int = Field(ge=16, le=16384)
    maximum_indirect_link_depth: Literal[0]
    target_coverage: Literal["SEPARATE_ORDINARY_ROOT_WALK"]
    known_fields_required: Literal[True]
    identity: Identity


class CoverageAssessment(Sealed):
    namespace = "policy-coverage-assessment:v2"
    contract_version: Literal["policy-coverage-assessment-v2"]
    scope: EvidenceRef
    measurement: EvidenceRef
    original_after: EvidenceRef
    baseline: EvidenceRef
    known_recorded_fields: Literal["MATCH"]
    historical_coverage: Literal["UNKNOWN"]
    historical_object_continuity: Literal["NOT_ESTABLISHED"]
    hidden_contents: Literal["UNKNOWN_NOT_RETROACTIVELY_COMPLETED"]
    compared_field_count: int = Field(ge=1)
    expansions: tuple[dict, ...]
    new_metadata: tuple[dict, ...]
    acquisition_accounting: tuple[dict, ...]
    identity: Identity


def relative_parts(value: str) -> tuple[str, ...]:
    if value == ".":
        return ()
    if (
        not isinstance(value, str)
        or not value
        or len(value) > 32760
        or "\\" in value
        or ":" in value
        or any(ord(c) < 32 for c in value)
    ):
        raise ValueError("ambiguous relative evidence path")
    parts = value.split("/")
    if any(
        not p
        or p in {".", ".."}
        or p.endswith((".", " "))
        or any(char in p for char in ':*?"<>|')
        or PureWindowsPath(p).is_reserved()
        for p in parts
    ):
        raise ValueError("ambiguous relative evidence component")
    return tuple(parts)


def local_path(value: str) -> PureWindowsPath:
    p = PureWindowsPath(value)
    if (
        not re.fullmatch(r"[A-Za-z]:", p.drive)
        or not p.is_absolute()
        or any(
            x in {".", ".."}
            or x.endswith((".", " "))
            or any(char in x for char in ':*?"<>|')
            or PureWindowsPath(x).is_reserved()
            or any(ord(c) < 32 for c in x)
            for x in p.parts[1:]
        )
    ):
        raise ValueError("unambiguous local drive path required")
    # Reject normalization which would hide redundant separators or dot components.
    if str(p).casefold() != value.replace("/", "\\").casefold():
        raise ValueError("noncanonical local evidence path")
    return p


def parse_mount_point(raw: bytes) -> str:
    """Parse only the bounded Microsoft mount-point layout, never arbitrary tags."""
    if not 16 <= len(raw) <= 16384:
        raise ValueError("reparse buffer length outside contract")
    tag, length, reserved = struct.unpack_from("<IHH", raw)
    if tag != MOUNT_POINT_TAG or reserved != 0 or length + 8 != len(raw):
        raise ValueError("unsupported reparse tag or inconsistent buffer length")
    so, sl, po, pl = struct.unpack_from("<HHHH", raw, 8)
    data = raw[16:]
    for offset, count in ((so, sl), (po, pl)):
        if offset % 2 or count % 2 or not count or offset + count > len(data):
            raise ValueError("invalid reparse name offsets")
    try:
        substitute = data[so : so + sl].decode("utf-16-le", errors="strict")
        display = data[po : po + pl].decode("utf-16-le", errors="strict")
    except UnicodeError as error:
        raise ValueError("invalid reparse name encoding") from error
    if not substitute.startswith("\\??\\") or "\0" in substitute + display:
        raise ValueError("unsupported reparse target namespace")
    target = substitute[4:]
    local_path(target)
    if display and display.casefold() != target.casefold():
        raise ValueError("ambiguous reparse display/substitute target")
    return target


def _object_identity(value):
    if (
        not isinstance(value, dict)
        or set(value) != {"volume_serial", "file_index"}
        or not re.fullmatch("[0-9a-f]{8}", str(value["volume_serial"]))
        or not re.fullmatch("[0-9a-f]{16}", str(value["file_index"]))
        or int(value["file_index"], 16) == 0
    ):
        raise ValueError("complete native object/volume identity required")


def _acyclic_directory_graph(rows):
    """Detect namespace cycles through ordinary subtrees AND declared links."""
    graph = {r["path"]: [] for r in rows if r["kind"] == "directory"}
    for row in rows:
        parts = relative_parts(row["path"])
        if not parts:
            continue
        parent = "/".join(parts[:-1]) or "."
        if row["kind"] == "directory":
            graph[parent].append(row["path"])
        elif row["kind"] == "reparse":
            graph[parent].append(row["target_relative_path"])
    colors = {}
    for origin in graph:
        if colors.get(origin) == 2:
            continue
        stack = [(origin, False)]
        while stack:
            node, leaving = stack.pop()
            if leaving:
                colors[node] = 2
                continue
            if colors.get(node) == 1:
                raise ValueError("declared current namespace has a reparse cycle")
            if colors.get(node) == 2:
                continue
            colors[node] = 1
            stack.append((node, True))
            stack.extend((target, False) for target in graph[node])


def validate_current_inventory(value: dict, scope: object, measurement: object) -> dict:
    """Independent full schema/coverage checks; physical authenticity is separate."""
    scope, measurement = HistoricalCoverageScope.read(scope), CoverageMeasurement.read(measurement)
    if (
        measurement.scope != artifact_ref(scope)
        or not isinstance(value, dict)
        or set(value)
        != {
            "contract_version",
            "scope",
            "measurement",
            "candidate",
            "instance",
            "phase",
            "roots",
            "fixture_only",
            "capture",
            "identity",
        }
        or value["contract_version"] != "policy-coverage-inventory-v2"
        or EvidenceRef.read(value["scope"]) != artifact_ref(scope)
        or EvidenceRef.read(value["measurement"]) != artifact_ref(measurement)
        or Candidate.read(value["candidate"]) != measurement.candidate
        or value["phase"] not in {"before", "after"}
        or type(value["fixture_only"]) is not bool
        or value["fixture_only"] != scope.fixture_only
        or not re.fullmatch(r"(?:test|operational)-[0-9]{8}-[0-9]{3}", str(value["instance"]))
    ):
        raise ValueError("current version/scope/measurement/candidate/instance binding mismatch")
    if value["identity"] != content_identity(
        CURRENT_NAMESPACE, {k: v for k, v in value.items() if k != "identity"}
    ):
        raise ValueError("current inventory canonical identity mismatch")
    if tuple(r.get("path") for r in value["roots"]) != scope.roots:
        raise ValueError("current inventory omits or changes declared protected roots")
    declared_links = {(x.root, x.row["path"]) for x in scope.links}
    count = read_bytes = 0
    common = {
        "path",
        "kind",
        "attributes",
        "mtime_ns",
        "ctime_ns",
        "descriptor_sha256",
        "object_identity",
    }
    for root in value["roots"]:
        root_path = local_path(root["path"])
        if (
            set(root)
            != {
                "path",
                "entries",
                "file_count",
                "unreadable_count",
                "security_descriptors_requested",
                "inventory_identity",
            }
            or root["security_descriptors_requested"] is not True
            or type(root["unreadable_count"]) is not int
            or root["unreadable_count"] != 0
            or type(root["file_count"]) is not int
            or root["inventory_identity"]
            != content_identity("policy-coverage-root:v2", root["entries"])
        ):
            raise ValueError("incomplete current root or root identity mismatch")
        rows = root["entries"]
        names = [r.get("path") for r in rows]
        if (
            not names
            or names != sorted(set(names), key=lambda x: x.casefold())
            or len({x.casefold() for x in names}) != len(names)
        ):
            raise ValueError("current paths duplicated, aliased, missing or unordered")
        by_path = {r["path"]: r for r in rows}
        object_keys = []
        if by_path.get(".", {}).get("kind") != "directory":
            raise ValueError("complete ordinary root row required")
        for row in rows:
            parts = relative_parts(row["path"])
            if len(parts) > measurement.maximum_depth:
                raise ValueError("current directory depth exceeds finite contract")
            if parts and by_path.get("/".join(parts[:-1]) or ".", {}).get("kind") != "directory":
                raise ValueError("current path parent is missing or an alias")
            kind = row.get("kind")
            extra = (
                {"byte_length", "sha256"}
                if kind == "file"
                else {
                    "size",
                    "tag",
                    "reparse_data_base64",
                    "target_relative_path",
                    "target_object_identity",
                    "target_root_identity",
                }
                if kind == "reparse"
                else set()
            )
            if kind not in {"file", "directory", "reparse"} or set(row) != common | extra:
                raise ValueError("unreadable, missing, unsupported or incomplete current row")
            if (
                type(row["attributes"]) is not int
                or not 0 <= row["attributes"] <= 0xFFFFFFFF
                or type(row["mtime_ns"]) is not int
                or type(row["ctime_ns"]) is not int
                or not re.fullmatch("[0-9a-f]{64}", str(row["descriptor_sha256"]))
            ):
                raise ValueError("required current raw/security coverage missing")
            _object_identity(row["object_identity"])
            object_keys.append(
                (row["object_identity"]["volume_serial"], row["object_identity"]["file_index"])
            )
            if bool(row["attributes"] & 0x400) != (kind == "reparse") or bool(
                row["attributes"] & 16
            ) != (kind in {"directory", "reparse"}):
                raise ValueError("current raw attribute/kind contradiction")
            if kind == "file":
                if (
                    type(row["byte_length"]) is not int
                    or row["byte_length"] < 0
                    or not re.fullmatch("[0-9a-f]{64}", str(row["sha256"]))
                ):
                    raise ValueError("complete current file byte pin required")
                read_bytes += row["byte_length"]
            if kind == "reparse":
                if (
                    (root["path"], row["path"]) not in declared_links
                    or row["tag"] not in measurement.supported_reparse_tags
                    or type(row["size"]) is not int
                    or row["size"] < 0
                ):
                    raise ValueError("new, undeclared or unsupported reparse object")
                try:
                    raw = base64.b64decode(row["reparse_data_base64"], validate=True)
                except (ValueError, TypeError) as error:
                    raise ValueError("invalid reparse buffer encoding") from error
                if len(raw) > measurement.maximum_reparse_bytes:
                    raise ValueError("reparse bytes exceed measurement bound")
                target = local_path(parse_mount_point(raw))
                if not target.is_relative_to(root_path):
                    raise ValueError("reparse target escapes exact protected root")
                relative = target.relative_to(root_path).as_posix()
                if relative != row["target_relative_path"]:
                    raise ValueError("reparse target relationship differs from raw buffer")
                target_row = by_path.get(relative)
                if target_row is None or target_row["kind"] != "directory":
                    raise ValueError("dangling, cyclic, indirect or unmeasured reparse target")
                for item in (row["target_object_identity"], row["target_root_identity"]):
                    _object_identity(item)
                if (
                    row["target_object_identity"] != target_row["object_identity"]
                    or row["target_root_identity"] != by_path["."]["object_identity"]
                    or row["object_identity"]["volume_serial"]
                    != row["target_root_identity"]["volume_serial"]
                    or row["target_object_identity"]["volume_serial"]
                    != row["target_root_identity"]["volume_serial"]
                ):
                    raise ValueError("reparse containment object/volume proof mismatch")
            count += 1
        if root["file_count"] != sum(r["kind"] == "file" for r in rows):
            raise ValueError("current complete file count mismatch")
        if len(set(object_keys)) != len(object_keys):
            raise ValueError("current inventory contains an object alias")
        _acyclic_directory_graph(rows)
    capture = value["capture"]
    fields = {
        "contract_version",
        "origin",
        "nonce",
        "started_utc",
        "finished_utc",
        "collector_pin",
        "runtime_pin",
        "method",
        "complete",
        "errors",
        "observed_entries",
        "read_bytes",
        "logical_bytes",
        "observation_identity",
    }
    generated = (
        value["fixture_only"] is True
        and isinstance(capture, dict)
        and capture.get("origin") == "GENERATED_FIXTURE_NOT_AN_OBSERVATION"
        and capture.get("method") == "generated-coverage-schema-vector-v2"
    )
    native = (
        isinstance(capture, dict)
        and capture.get("origin") == "NEW_OBSERVATION"
        and capture.get("method") == "held-local-objects-and-contained-mount-point-v2"
    )
    if (
        not isinstance(capture, dict)
        or set(capture) != fields
        or capture["contract_version"] != "policy-coverage-acquisition-v2"
        or not (generated or native)
        or capture["complete"] is not True
        or capture["errors"] != []
        or not re.fullmatch("[0-9a-f]{32}", str(capture["nonce"]))
        or type(capture["observed_entries"]) is not int
        or type(capture["read_bytes"]) is not int
        or type(capture["logical_bytes"]) is not int
        or capture["logical_bytes"] != read_bytes
        or capture["observed_entries"] != count
        or capture["read_bytes"] != (0 if generated else read_bytes)
        or count > measurement.maximum_entries
        or read_bytes > measurement.maximum_read_bytes
        or not 0
        <= (utc(capture["finished_utc"]) - utc(capture["started_utc"])).total_seconds()
        <= measurement.maximum_seconds
        or capture["observation_identity"]
        != content_identity(
            "policy-coverage-fixture-acquisition:v2"
            if generated
            else "policy-coverage-observation:v2",
            {k: v for k, v in capture.items() if k != "observation_identity"},
        )
    ):
        raise ValueError("current acquisition completeness/method/time/provenance mismatch")
    BytePin.read(capture["collector_pin"])
    BytePin.read(capture["runtime_pin"])
    return value


def compare_known_fields(
    scope, before_bytes: bytes, after_bytes: bytes, baseline: dict, measurement
) -> CoverageAssessment:
    scope = validate_scope(scope, before_bytes, after_bytes)
    measurement = CoverageMeasurement.read(measurement)
    baseline = validate_current_inventory(baseline, scope, measurement)
    after, _ = read_historical(after_bytes)
    if baseline["phase"] != "before":
        raise ValueError("prospective B0 must be a distinct BEFORE acquisition")
    expansions, new_metadata, accounting = [], [], []
    compared = 0
    for root_index, (old, new) in enumerate(zip(after["roots"], baseline["roots"], strict=True)):
        current = {r["path"]: r for r in new["entries"]}
        known_names = {r["path"] for r in old["entries"]}
        gaps = {x.row["path"] for x in scope.gaps if x.root == old["path"]}
        known_keys = {}
        for row in old["entries"]:
            key = row["path"]
            observed = current.get(key)
            if observed is None:
                raise ValueError("known historical path omitted: " + old["path"] + " / " + key)
            state = {k: v for k, v in row.items() if k != "security_read_error"}
            if row["kind"] == "unreadable_directory":
                state["kind"] = "directory"
            for field, value in state.items():
                if (
                    field not in observed
                    or type(observed[field]) is not type(value)
                    or observed[field] != value
                ):
                    raise ValueError(
                        "known recorded field drift: " + old["path"] + " / " + key + " / " + field
                    )
                compared += 1
                known_keys.setdefault(key, set()).add(field)
        for entry_index, row in enumerate(new["entries"]):
            key = row["path"]
            if key not in known_names:
                parts = relative_parts(key)
                if not any(
                    parts[: len(relative_parts(g))] == relative_parts(g)
                    and len(parts) > len(relative_parts(g))
                    for g in gaps
                ):
                    raise ValueError(
                        "unexplained new path outside exact historical coverage gaps: " + key
                    )
                expansions.append(
                    {
                        "root": new["path"],
                        "path": key,
                        "kind": row["kind"],
                        "row_pin": BytePin.of(canonical_bytes(row)).model_dump(mode="json"),
                    }
                )
            extra = sorted(set(row) - known_keys.get(key, set()))
            if extra:
                # Every index resolves through the complete pinned B0. No known
                # or new field is dropped to reduce repeated path/digest text.
                new_metadata.append(
                    {"root_index": root_index, "entry_index": entry_index, "fields": extra}
                )
        accounting.append(
            {
                "root": old["path"],
                "original_root_identity": old["inventory_identity"],
                "baseline_root_identity": new["inventory_identity"],
                "original_file_count": old["file_count"],
                "baseline_file_count": new["file_count"],
                "original_unreadable_count": old["unreadable_count"],
                "baseline_unreadable_count": 0,
                "original_security_descriptors_requested": old["security_descriptors_requested"],
                "baseline_security_descriptors_requested": True,
                "failure_records_retained_in_scope": [
                    x.row_index for x in scope.gaps if x.root == old["path"]
                ],
                "security_failures_retained": [
                    i for i, r in enumerate(old["entries"]) if "security_read_error" in r
                ],
            }
        )
    return CoverageAssessment.seal(
        contract_version="policy-coverage-assessment-v2",
        scope=artifact_ref(scope),
        measurement=artifact_ref(measurement),
        original_after=scope.after,
        baseline=artifact_ref(baseline),
        known_recorded_fields="MATCH",
        historical_coverage="UNKNOWN",
        historical_object_continuity="NOT_ESTABLISHED",
        hidden_contents="UNKNOWN_NOT_RETROACTIVELY_COMPLETED",
        compared_field_count=compared,
        expansions=tuple(expansions),
        new_metadata=tuple(new_metadata),
        acquisition_accounting=tuple(accounting),
    )


def compare_current_inventories(before: dict, after: dict, scope, measurement) -> dict:
    before = validate_current_inventory(before, scope, measurement)
    after = validate_current_inventory(after, scope, measurement)
    if (
        before["instance"] != after["instance"]
        or before["capture"]["observation_identity"] == after["capture"]["observation_identity"]
        or before["capture"]["nonce"] == after["capture"]["nonce"]
        or utc(after["capture"]["started_utc"]) < utc(before["capture"]["finished_utc"])
    ):
        raise ValueError("current acquisitions reused, reordered or instance-mismatched")
    changed = [a["path"] for a, b in zip(before["roots"], after["roots"], strict=True) if a != b]
    return {
        "equal": not changed,
        "changed_roots": changed,
        "historical_coverage": "UNKNOWN",
        "historical_object_continuity": "NOT_ESTABLISHED",
        "execution_authorized": False,
    }

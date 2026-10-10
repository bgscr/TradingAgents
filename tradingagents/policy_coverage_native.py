"""No-follow local object acquisition for explicit directory/mixed-root contracts.

No service, credential, backup privilege or repair APIs are used. Link support
is deliberately confined to measurement, not executable/artifact path admission.
"""

from __future__ import annotations

import base64
import ctypes
import hashlib
import os
import stat
import time
import uuid
from contextlib import ExitStack, contextmanager
from datetime import datetime, timezone
from pathlib import Path

from tradingagents.policy_coverage_contract import (
    artifact_ref,
    coverage_capture_method,
    expected_root_kind,
    local_path,
    parse_mount_point,
    read_coverage_context,
    relative_parts,
    validate_current_inventory,
)
from tradingagents.policy_evidence_acquisition import (
    _chunks,
    _file_api,
    _security,
    new_evidence_output,
)
from tradingagents.policy_qualified_contract import BytePin, canonical_bytes, content_identity


def _information(handle):
    api, Info = _file_api()
    info = Info()
    if not api.GetFileInformationByHandle(handle, ctypes.byref(info)):
        raise ctypes.WinError(ctypes.get_last_error())
    return info


def _available_local(path):
    """Metadata-only guard against network drives and recall/hydration objects."""
    from ctypes import wintypes

    api, _ = _file_api()
    api.GetDriveTypeW.argtypes = [wintypes.LPCWSTR]
    api.GetDriveTypeW.restype = wintypes.UINT
    if api.GetDriveTypeW(path.anchor) != 3:
        raise ValueError("coverage acquisition requires an existing local fixed volume")
    before = path.lstat()
    if before.st_file_attributes & (0x1000 | 0x40000 | 0x400000):
        raise ValueError("offline/recall objects require forbidden hydration")
    return before


@contextmanager
def _held(path, directory):
    """Ordinary measurement handle with no-recall and unchanged alias protections."""
    before = _available_local(path)
    if (
        before.st_file_attributes & 0x400
        or stat.S_ISDIR(before.st_mode) != directory
        or (not directory and (not stat.S_ISREG(before.st_mode) or before.st_nlink != 1))
    ):
        raise ValueError("reparse/aliased/unsupported ordinary measurement object")
    api, _ = _file_api()
    # Metadata-only directory access does not establish the data-sharing gate:
    # rename/FSCTL_SET_REPARSE_POINT could otherwise redirect pathname children.
    handle = api.CreateFileW(str(path), 0x80000000, 1, None, 3, 0x02300000, None)
    if handle == ctypes.c_void_p(-1).value:
        raise ctypes.WinError(ctypes.get_last_error())
    try:
        info = _information(handle)
        if (
            info.attributes & (0x400 | 0x1000 | 0x40000 | 0x400000)
            or bool(info.attributes & 16) != directory
            or (not directory and info.links != 1)
            or ((info.index_high << 32) | info.index_low) != before.st_ino
        ):
            raise ValueError("opened measurement object changed type/identity/availability")
        original = _identity(handle)
        security_before = _security(handle)
        yield handle, before, security_before
        after = _available_local(path)
        if _security(handle) != security_before:
            raise ValueError("held ordinary security descriptor changed during acquisition")
        if _identity(handle) != original or (
            before.st_dev,
            before.st_ino,
            before.st_size,
            before.st_mtime_ns,
            before.st_ctime_ns,
            before.st_file_attributes,
        ) != (
            after.st_dev,
            after.st_ino,
            after.st_size,
            after.st_mtime_ns,
            after.st_ctime_ns,
            after.st_file_attributes,
        ):
            raise ValueError("held measurement object changed during acquisition")
    finally:
        api.CloseHandle(handle)


@contextmanager
def _ancestors(path):
    if len(path.parents) > 128:
        raise ValueError("finite ordinary ancestor depth exhausted")
    with ExitStack() as stack:
        for parent in reversed(path.parents):
            stack.enter_context(_held(parent, True))
        yield


def _identity(handle):
    info = _information(handle)
    return {
        "volume_serial": f"{info.volume:08x}",
        "file_index": f"{(info.index_high << 32) | info.index_low:016x}",
    }


def _metadata(handle, before, expected_security=None):
    info = _information(handle)
    if (
        (info.index_high << 32) | info.index_low
    ) != before.st_ino or info.attributes != before.st_file_attributes:
        raise ValueError("native opened object differs from named observation")
    security = _security(handle)
    if expected_security is not None and security != expected_security:
        raise ValueError("ordinary security descriptor changed before observation")
    return {
        "attributes": info.attributes,
        "mtime_ns": before.st_mtime_ns,
        "ctime_ns": before.st_ctime_ns,
        "descriptor_sha256": security,
        "object_identity": _identity(handle),
    }


def _read_reparse(handle):
    from ctypes import wintypes

    api, _ = _file_api()
    api.DeviceIoControl.argtypes = [
        wintypes.HANDLE,
        wintypes.DWORD,
        ctypes.c_void_p,
        wintypes.DWORD,
        ctypes.c_void_p,
        wintypes.DWORD,
        ctypes.POINTER(wintypes.DWORD),
        ctypes.c_void_p,
    ]
    api.DeviceIoControl.restype = wintypes.BOOL
    buffer = ctypes.create_string_buffer(16384)
    count = wintypes.DWORD()
    if not api.DeviceIoControl(
        handle, 0x000900A8, None, 0, buffer, len(buffer), ctypes.byref(count), None
    ):
        raise ctypes.WinError(ctypes.get_last_error())
    raw = buffer.raw[: count.value]
    parse_mount_point(raw)
    return raw


@contextmanager
def _link_object(path):
    """Hold actual reparse object without traversing it or sharing write/delete."""
    api, _ = _file_api()
    before = _available_local(path)
    if not before.st_file_attributes & 0x400:
        raise ValueError("declared link is not a reparse object")
    handle = api.CreateFileW(str(path), 0x80020080, 1, None, 3, 0x02300000, None)
    if handle == ctypes.c_void_p(-1).value:
        raise ctypes.WinError(ctypes.get_last_error())
    try:
        info = _information(handle)
        if (
            not info.attributes & 0x400
            or not info.attributes & 16
            or info.links != 1
            or info.attributes & (0x1000 | 0x40000 | 0x400000)
        ):
            raise ValueError("opened link type/alias mismatch")
        metadata = _metadata(handle, before)
        raw = _read_reparse(handle)
        yield handle, before, metadata, raw
        after = path.lstat()
        if (
            (
                before.st_dev,
                before.st_ino,
                before.st_size,
                before.st_mtime_ns,
                before.st_ctime_ns,
                before.st_file_attributes,
            )
            != (
                after.st_dev,
                after.st_ino,
                after.st_size,
                after.st_mtime_ns,
                after.st_ctime_ns,
                after.st_file_attributes,
            )
            or _read_reparse(handle) != raw
            or _metadata(handle, after) != metadata
        ):
            raise ValueError("link object or reparse bytes changed during held acquisition")
    finally:
        api.CloseHandle(handle)


def _hold_chain(stack, root, path, *, maximum_depth=128):
    relative = path.relative_to(root)
    if len(relative.parts) > maximum_depth:
        raise ValueError("native containment depth exceeds contract")
    current = root
    final = None
    for part in relative.parts:
        relative_parts(part)
        current = current / part
        try:
            final = stack.enter_context(_held(current, True))
        except ValueError as error:
            raise ValueError(
                "indirect/cyclic/aliased containment component is forbidden"
            ) from error
    return final


def measure_junction(root: Path, path: Path, *, maximum_depth: int = 128) -> dict:
    """Measure link-object bytes and an independently opened ordinary target.

    Actual handles supplement lexical containment. A target prefix or resolved
    string alone is never reported as proof. Zero indirect-link depth is supported.
    """
    if os.name != "nt" or not 1 <= maximum_depth <= 128:
        raise ValueError("bounded local Windows measurement required")
    root = Path(str(local_path(str(root.absolute()))))
    path = Path(str(local_path(str(path.absolute()))))
    if path == root or not path.is_relative_to(root):
        raise ValueError("link is outside exact measurement root")
    with ExitStack() as stack:
        stack.enter_context(_ancestors(root))
        root_handle, _, _ = stack.enter_context(_held(root, True))
        _hold_chain(stack, root, path.parent, maximum_depth=maximum_depth)
        handle, before, metadata, raw = stack.enter_context(_link_object(path))
        target = Path(parse_mount_point(raw))
        if not target.is_relative_to(root):
            raise ValueError("reparse target escapes exact protected root")
        if target == path or path.parent == target or path.parent.is_relative_to(target):
            raise ValueError("reparse target cycle")
        held_target = _hold_chain(stack, root, target, maximum_depth=maximum_depth)
        target_handle = root_handle if held_target is None else held_target[0]
        root_identity, target_identity = _identity(root_handle), _identity(target_handle)
        if (
            root_identity["volume_serial"] != target_identity["volume_serial"]
            or root_identity["volume_serial"] != metadata["object_identity"]["volume_serial"]
        ):
            raise ValueError("link containment spans a different volume")
        if target_identity == metadata["object_identity"]:
            raise ValueError("native link target cycle")
        return {
            "path": path.relative_to(root).as_posix(),
            "kind": "reparse",
            **metadata,
            "size": before.st_size,
            "tag": 0xA0000003,
            "reparse_data_base64": base64.b64encode(raw).decode("ascii"),
            "target_relative_path": target.relative_to(root).as_posix(),
            "target_object_identity": target_identity,
            "target_root_identity": root_identity,
        }


def capture_coverage(
    scope, measurement, output: Path, *, instance: str, phase: str, fixture_only: bool
) -> dict:
    """Capture fresh explicit roots once; failed observations remain unadmitted."""
    import sys

    scope, measurement = read_coverage_context(scope, measurement)
    if (
        measurement.scope != artifact_ref(scope)
        or scope.fixture_only != fixture_only
        or phase not in {"before", "after"}
    ):
        raise ValueError("capture scope/measurement/fixture/phase mismatch")
    roots = [Path(str(local_path(name))) for name in scope.roots]
    output = output.absolute()
    if any(output == r or output.is_relative_to(r) for r in roots):
        raise ValueError("publish observations outside every measured root")
    if output.exists():
        raise FileExistsError("new additive evidence output required")
    started = time.monotonic()
    start = datetime.now(timezone.utc).isoformat()
    collector = BytePin.of(Path(__file__).read_bytes())
    runtime = BytePin.of(Path(sys.executable).read_bytes())
    declared = {(r.root, r.row["path"]) for r in scope.links}
    count = read_bytes = 0
    discovered = len(roots)
    errors = []
    inventories = []

    def budget():
        if time.monotonic() - started > measurement.maximum_seconds:
            raise TimeoutError("finite capture time exhausted")
        if (
            max(count, discovered) > measurement.maximum_entries
            or read_bytes > measurement.maximum_read_bytes
        ):
            raise ValueError("finite capture entry/byte budget exhausted")

    for root_index, root in enumerate(roots):
        try:
            budget()
        except (ValueError, TimeoutError) as error:
            errors.append(
                {
                    "capture_stopped": str(error),
                    "first_unvisited_root": str(root),
                    "remaining_roots_not_visited": len(roots) - root_index,
                }
            )
            break
        rows = []

        def children(path, *, reserve):
            nonlocal discovered
            found = []
            with os.scandir(path) as entries:
                for entry in entries:
                    budget()
                    if reserve:
                        discovered += 1
                    if (
                        discovered > measurement.maximum_entries
                        or len(found) >= measurement.maximum_entries
                    ):
                        raise ValueError("finite directory enumeration entry budget exhausted")
                    found.append(Path(entry.path))
            return sorted(found, key=lambda item: item.name.casefold())

        def walk(path, depth=0, *, root=root, rows=rows, root_index=root_index):
            nonlocal count, read_bytes
            budget()
            if depth > measurement.maximum_depth:
                raise ValueError("finite capture depth exhausted")
            relative = "." if path == root else path.relative_to(root).as_posix()
            try:
                info = _available_local(path)
                if depth == 0 and (
                    info.st_file_attributes & 0x400
                    or ("directory" if stat.S_ISDIR(info.st_mode) else "file")
                    != expected_root_kind(scope, root_index)
                ):
                    raise ValueError("actual ordinary root type differs from trusted scope")
                if info.st_file_attributes & 0x400:
                    if (str(root), relative) not in declared:
                        raise ValueError("new or undeclared reparse object")
                    rows.append(
                        measure_junction(root, path, maximum_depth=measurement.maximum_depth)
                    )
                    count += 1
                    budget()
                    return
                directory = stat.S_ISDIR(info.st_mode)
                with _held(path, directory) as (handle, before, security_before):
                    row = {
                        "path": relative,
                        "kind": "directory" if directory else "file",
                        **_metadata(handle, before, security_before),
                    }
                    if directory:
                        rows.append(row)
                        count += 1
                        budget()
                        initial_children = children(path, reserve=True)
                        for child in initial_children:
                            walk(child, depth + 1)
                        after_children = children(path, reserve=False)
                        if [p.name for p in after_children] != [p.name for p in initial_children]:
                            raise ValueError("directory membership changed during capture")
                    else:
                        digest = hashlib.sha256()
                        size = 0
                        for chunk in _chunks(handle):
                            size += len(chunk)
                            read_bytes += len(chunk)
                            budget()
                            digest.update(chunk)
                        if size != before.st_size:
                            raise ValueError("file length changed during capture")
                        row.update(byte_length=size, sha256=digest.hexdigest())
                        rows.append(row)
                        count += 1
                        budget()
            except (OSError, ValueError, TimeoutError) as error:
                rows.append(
                    {
                        "path": relative,
                        "kind": "unreadable_object",
                        "error_type": type(error).__name__,
                        "error_code": getattr(error, "winerror", None)
                        or getattr(error, "errno", None),
                    }
                )
                errors.append({"root": str(root), "path": relative, "error": str(error)})
                if isinstance(error, TimeoutError):
                    raise

        try:
            with _ancestors(root):
                walk(root)
        except (OSError, ValueError, TimeoutError) as error:
            errors.append({"root": str(root), "error": str(error)})
        rows.sort(key=lambda row: (row["path"].casefold(), row["kind"]))
        inventories.append(
            {
                "path": str(root),
                "entries": rows,
                "file_count": sum(r["kind"] == "file" for r in rows),
                "unreadable_count": sum(r["kind"].startswith("unreadable") for r in rows),
                "security_descriptors_requested": True,
                "inventory_identity": content_identity("policy-coverage-root:v2", rows),
            }
        )
    if BytePin.of(Path(__file__).read_bytes()) != collector:
        errors.append({"collector": "source changed during capture"})
    capture = {
        "contract_version": "policy-coverage-acquisition-v2",
        "origin": "NEW_OBSERVATION",
        "nonce": uuid.uuid4().hex,
        "started_utc": start,
        "finished_utc": datetime.now(timezone.utc).isoformat(),
        "collector_pin": collector.model_dump(mode="json"),
        "runtime_pin": runtime.model_dump(mode="json"),
        "method": coverage_capture_method(scope),
        "complete": not errors,
        "errors": errors,
        "observed_entries": count,
        "read_bytes": read_bytes,
        "logical_bytes": sum(
            r.get("byte_length", 0) for root in inventories for r in root["entries"]
        ),
    }
    capture["observation_identity"] = content_identity("policy-coverage-observation:v2", capture)
    value = {
        "contract_version": "policy-coverage-inventory-v2",
        "scope": artifact_ref(scope).model_dump(mode="json"),
        "measurement": artifact_ref(measurement).model_dump(mode="json"),
        "candidate": measurement.candidate.model_dump(mode="json"),
        "instance": instance,
        "phase": phase,
        "roots": inventories,
        "fixture_only": fixture_only,
        "capture": capture,
    }
    value["identity"] = content_identity("policy-coverage-inventory:v2", value)
    if not errors:
        try:
            validate_current_inventory(value, scope, measurement)
        except ValueError as error:
            errors.append({"complete_inventory_validation": str(error)})
            capture["complete"] = False
            capture.pop("observation_identity")
            capture["observation_identity"] = content_identity(
                "policy-coverage-observation:v2", capture
            )
            value.pop("identity")
            value["identity"] = content_identity("policy-coverage-inventory:v2", value)
    raw = canonical_bytes(value) + b"\n"
    if len(raw) > 256 * 1024**2:
        raise ValueError("complete captured artifact exceeds capacity")
    with new_evidence_output(output) as stream:
        stream.write(raw)
    return value

"""Additive evidence acquisition. Observations and inventories confer no authority.

Historical root row shapes are preserved. Collection provenance lives outside
those rows, so a fresh acquisition can compare to an original without pretending
to be the original acquisition. No hidden context.py or working directory input.
"""

from __future__ import annotations

import base64
import ctypes
import hashlib
import os
import re
import stat
import subprocess
import sys
import time
import uuid
from contextlib import contextmanager
from datetime import datetime, timezone
from functools import lru_cache
from pathlib import Path
from typing import Literal

from pydantic import Field, model_validator
from typing_extensions import Self

from tradingagents.policy_qualified_contract import (
    Candidate,
    Sealed,
    Text,
    canonical_bytes,
    content_identity,
    utc,
)


def _now():
    return datetime.now(timezone.utc).isoformat()


def _publish(output: Path, raw: bytes):
    if len(raw) > 256 * 1024**2:
        raise ValueError("complete evidence exceeds the supported artifact byte bound")
    with new_evidence_output(output) as stream:
        stream.write(raw)


@contextmanager
def new_evidence_output(output: Path):
    """Hold unaliased output ancestors; publish once, retaining interrupted bytes."""
    if os.name != "nt":
        raise ValueError("no-replace Windows publication required")
    output = output.absolute()
    if not re.fullmatch(r"[A-Za-z]:", output.drive) or any(
        part in {".", ".."}
        or part.endswith((".", " "))
        or ":" in part
        or any(ord(char) < 32 for char in part)
        for part in output.parts[1:]
    ):
        raise ValueError("unambiguous local publication path required")
    with _ancestors(output, stable_metadata=False):
        if output.exists():
            raise FileExistsError("evidence output already exists")
        partial = output.with_name(output.name + ".partial")
        # Keep the exact created object open through publication. No closing and
        # reopening a checked path, nor weakening the held ancestor handles.
        import msvcrt

        api, Info = _file_api()
        handle = api.CreateFileW(str(partial), 0xC0010000, 1, None, 1, 0x02200000, None)
        if handle == ctypes.c_void_p(-1).value:
            raise ctypes.WinError(ctypes.get_last_error())
        try:
            fd = msvcrt.open_osfhandle(handle, os.O_RDWR | os.O_BINARY)
        except BaseException:
            api.CloseHandle(handle)
            raise
        try:
            stream = os.fdopen(fd, "w+b")
        except BaseException:
            os.close(fd)
            raise
        with stream:
            info = Info()
            if not api.GetFileInformationByHandle(handle, ctypes.byref(info)):
                raise ctypes.WinError(ctypes.get_last_error())
            if info.attributes & (0x400 | 16) or info.links != 1:
                raise ValueError("created publication object is aliased or not a regular file")
            original = _ordinary(partial, False)
            yield stream
            stream.flush()
            os.fsync(stream.fileno())
            _rename_same_parent(handle, output.name)
            observed = _ordinary(output, False)
            if (
                observed.st_ino != ((info.index_high << 32) | info.index_low)
                or observed.st_dev != original.st_dev
            ):
                raise ValueError("published evidence does not name the held complete object")


def _rename_same_parent(handle, name: str) -> None:
    """NT basename-only rename, relative to the held source's existing parent.

    FILE_RENAME_INFORMATION differs from the Win32 FILE_RENAME_INFO wrapper:
    a basename with NULL RootDirectory names the existing source parent, not CWD.
    ReplaceIfExists is false. No target-directory reopen or sharing relaxation.
    https://learn.microsoft.com/windows-hardware/drivers/ddi/ntifs/ns-ntifs-_file_rename_information
    """
    from ctypes import wintypes

    if (
        not name
        or name in {".", ".."}
        or name.endswith((".", " "))
        or any(c in name for c in "\\/:\0")
        or any(ord(c) < 32 for c in name)
    ):
        raise ValueError("publication rename requires one unambiguous basename")
    encoded = name.encode("utf-16-le")
    if len(encoded) > 65532:
        raise ValueError("publication basename exceeds the finite native name bound")

    class Rename(ctypes.Structure):
        _fields_ = [
            ("replace", wintypes.DWORD),
            ("root", wintypes.HANDLE),
            ("length", wintypes.ULONG),
            ("name", wintypes.WCHAR * (len(encoded) // 2 + 1)),
        ]

    class Status(ctypes.Structure):
        _fields_ = [("status", ctypes.c_void_p), ("information", ctypes.c_size_t)]

    value = Rename()
    value.root = None
    value.length = len(encoded)
    value.name = name
    status = Status()
    native = ctypes.WinDLL("ntdll", use_last_error=True)
    rename = native.NtSetInformationFile
    rename.argtypes = [
        wintypes.HANDLE,
        ctypes.POINTER(Status),
        ctypes.c_void_p,
        wintypes.ULONG,
        ctypes.c_int,
    ]
    rename.restype = wintypes.LONG
    code = rename(handle, ctypes.byref(status), ctypes.byref(value), ctypes.sizeof(value), 10)
    if code != 0:
        native.RtlNtStatusToDosError.argtypes = [wintypes.LONG]
        native.RtlNtStatusToDosError.restype = wintypes.ULONG
        raise ctypes.WinError(native.RtlNtStatusToDosError(code))


def _ordinary(path: Path, directory: bool, *, pinned_tool: bool = False):
    info = path.lstat()
    if getattr(info, "st_file_attributes", 0) & 0x400 or stat.S_ISLNK(info.st_mode):
        raise ValueError("reparse evidence object rejected")
    if directory != stat.S_ISDIR(info.st_mode) or (
        not directory
        and (not stat.S_ISREG(info.st_mode) or (info.st_nlink != 1 and not pinned_tool))
    ):
        raise ValueError("aliased or unsupported evidence object")
    return info


@lru_cache(maxsize=1)
def _file_api():
    from ctypes import wintypes

    api = ctypes.WinDLL("kernel32", use_last_error=True)
    api.CreateFileW.argtypes = [
        wintypes.LPCWSTR,
        wintypes.DWORD,
        wintypes.DWORD,
        ctypes.c_void_p,
        wintypes.DWORD,
        wintypes.DWORD,
        wintypes.HANDLE,
    ]
    api.CreateFileW.restype = wintypes.HANDLE
    api.CloseHandle.argtypes = [wintypes.HANDLE]

    class Info(ctypes.Structure):
        _fields_ = [
            (name, wintypes.DWORD)
            for name in (
                "attributes",
                "create_low",
                "create_high",
                "access_low",
                "access_high",
                "write_low",
                "write_high",
                "volume",
                "size_high",
                "size_low",
                "links",
                "index_high",
                "index_low",
            )
        ]

    api.GetFileInformationByHandle.argtypes = [wintypes.HANDLE, ctypes.POINTER(Info)]
    api.GetFileInformationByHandle.restype = wintypes.BOOL
    api.ReadFile.argtypes = [
        wintypes.HANDLE,
        ctypes.c_void_p,
        wintypes.DWORD,
        ctypes.POINTER(wintypes.DWORD),
        ctypes.c_void_p,
    ]
    api.ReadFile.restype = wintypes.BOOL
    return api, Info


@contextmanager
def _held(path: Path, directory: bool, *, stable_metadata: bool = True, pinned_tool: bool = False):
    """Hold the actual Windows object against write/delete/alias substitution."""
    if os.name != "nt":
        raise ValueError("raw Windows acquisition requires Windows")
    api, Info = _file_api()
    before = _ordinary(path, directory, pinned_tool=pinned_tool)
    handle = api.CreateFileW(
        str(path),
        0x80000000,
        1,
        None,
        3,
        0x02200000,
        None,
    )
    if handle == ctypes.c_void_p(-1).value:
        raise ctypes.WinError(ctypes.get_last_error())
    try:
        info = Info()
        if not api.GetFileInformationByHandle(handle, ctypes.byref(info)):
            raise ctypes.WinError(ctypes.get_last_error())
        if (
            info.attributes & 0x400
            or bool(info.attributes & 16) != directory
            or (not directory and info.links != 1 and not pinned_tool)
        ):
            raise ValueError("opened evidence object has a reparse/alias/type mismatch")
        # Compare the opened object with lstat, rather than trusting a prior path check.
        index = (info.index_high << 32) | info.index_low
        if before.st_ino != index:
            raise ValueError("evidence path was substituted before open")
        yield handle, before
        after = _ordinary(path, directory, pinned_tool=pinned_tool)
        if (before.st_dev, before.st_ino) != (after.st_dev, after.st_ino):
            raise ValueError("held evidence object identity changed")
        if stable_metadata and (
            before.st_dev,
            before.st_ino,
            before.st_size,
            before.st_mtime_ns,
            before.st_ctime_ns,
            getattr(before, "st_file_attributes", 0),
        ) != (
            after.st_dev,
            after.st_ino,
            after.st_size,
            after.st_mtime_ns,
            after.st_ctime_ns,
            getattr(after, "st_file_attributes", 0),
        ):
            raise ValueError("evidence object changed during acquisition")
    finally:
        api.CloseHandle(handle)


@contextmanager
def _ancestors(path: Path, *, stable_metadata: bool = True):
    from contextlib import ExitStack

    with ExitStack() as stack:
        for parent in reversed(path.parents):
            stack.enter_context(_held(parent, True, stable_metadata=stable_metadata))
        yield


@lru_cache(maxsize=1)
def _security_api():
    from ctypes import wintypes

    api = ctypes.WinDLL("advapi32", use_last_error=True).GetKernelObjectSecurity
    api.argtypes = [
        wintypes.HANDLE,
        wintypes.DWORD,
        ctypes.c_void_p,
        wintypes.DWORD,
        ctypes.POINTER(wintypes.DWORD),
    ]
    api.restype = wintypes.BOOL
    return api


def _security(handle):
    from ctypes import wintypes

    api = _security_api()
    needed = wintypes.DWORD()
    api(handle, 7, None, 0, ctypes.byref(needed))
    if not 0 < needed.value <= 1024 * 1024:
        raise ctypes.WinError(ctypes.get_last_error())
    data = ctypes.create_string_buffer(needed.value)
    if not api(handle, 7, data, len(data), ctypes.byref(needed)):
        raise ctypes.WinError(ctypes.get_last_error())
    return hashlib.sha256(data.raw[: needed.value]).hexdigest()


def _chunks(handle):
    from ctypes import wintypes

    read = _file_api()[0].ReadFile
    buffer = ctypes.create_string_buffer(65536)
    count = wintypes.DWORD()
    while True:
        if not read(handle, buffer, len(buffer), ctypes.byref(count), None):
            raise ctypes.WinError(ctypes.get_last_error())
        if not count.value:
            return
        yield ctypes.string_at(buffer, count.value)


def compare_captures(before: dict, after: dict) -> dict:
    from tradingagents.policy_qualified_results import PolicyQualifiedVerifier

    for value in (before, after):
        identity = value.get("identity")
        if identity != content_identity(
            "crash-protection-historical-inventory:v1",
            {key: item for key, item in value.items() if key != "identity"},
        ):
            raise ValueError("capture semantic identity mismatch")
    a, b = PolicyQualifiedVerifier._roots(before), PolicyQualifiedVerifier._roots(after)
    failures = []
    if before.get("phase") != "before" or after.get("phase") != "after":
        failures.append("before/after acquisition phases are required")
    for label, value in (("before", before), ("after", after)):
        capture = value.get("capture")
        if (
            capture is not None and (capture.get("complete") is not True or capture.get("errors"))
        ) or value.get("unreadable_count", 0):
            failures.append(label + " acquisition is incomplete")
        if any(
            root.get("unreadable_count", 0)
            or any(row.get("kind") not in {"file", "directory"} for row in root["entries"])
            for root in value["roots"]
        ):
            failures.append(label + " has missing/unreadable/reparse/unsupported evidence")
    if after.get("equal") is False:
        failures.append("retained AFTER reports a blocking difference")
    changed = sorted(key for key in a.keys() | b.keys() if a.get(key) != b.get(key))
    result = {"equal": not changed and not failures, "changed_roots": changed}
    if failures:
        result["blocking_reasons"] = failures
    return result


def capture_roots(
    request: dict,
    output: Path,
    *,
    phase: str,
    previous: dict | None = None,
    maximum_seconds: int = 600,
    maximum_entries: int = 2_000_000,
    maximum_read_bytes: int = 64 * 1024**3,
) -> dict:
    """Capture exact explicit roots once, preserving missing/unstable observations."""
    if (
        set(request) != {"contract_version", "fixture_only", "roots"}
        or request["contract_version"] != "policy-evidence-root-list-v1"
        or type(request["fixture_only"]) is not bool
    ):
        raise ValueError("closed explicit root-list contract required")
    if phase not in {"before", "after"} or (phase == "after") != (previous is not None):
        raise ValueError("AFTER requires its exact retained BEFORE; BEFORE cannot reuse one")
    if (
        not 1 <= maximum_seconds <= 1800
        or not 1 <= maximum_entries <= 2_000_000
        or not 1 <= maximum_read_bytes <= 256 * 1024**3
    ):
        raise ValueError("capture exceeds finite time/entry/read-byte limits")
    roots = request["roots"]
    if not isinstance(roots, list) or not 1 <= len(roots) <= 2048:
        raise ValueError("bounded explicit root list required")
    paths = []
    for root in roots:
        if (
            set(root) != {"path", "security_descriptors_requested"}
            or type(root["security_descriptors_requested"]) is not bool
        ):
            raise ValueError("root declaration fields differ from measurement contract")
        path = Path(root["path"])
        if (
            not path.is_absolute()
            or ".." in path.parts
            or path.parent == path
            or not re.fullmatch(r"[A-Za-z]:", path.drive)
        ):
            raise ValueError("explicit local root, not a drive or relative path, required")
        paths.append(path)
    if len({str(path).casefold() for path in paths}) != len(paths):
        raise ValueError("duplicate capture root")
    output = output.absolute()
    if output.exists():
        raise FileExistsError("capture never overwrites an observation")
    if any(output.is_relative_to(path.absolute()) for path in paths):
        raise ValueError("capture output must be outside every observed scope")
    collector_pin = hashlib.sha256(Path(__file__).read_bytes()).hexdigest()
    runtime_pin = hashlib.sha256(Path(sys.executable).read_bytes()).hexdigest()
    started_utc = _now()
    started = time.monotonic()
    count = read_bytes = serialized_row_bytes = 0
    errors = []
    result = []

    def budget():
        if (
            time.monotonic() - started >= maximum_seconds
            or count >= maximum_entries
            or read_bytes > maximum_read_bytes
        ):
            raise TimeoutError("finite acquisition budget exhausted")

    for declaration in sorted(roots, key=lambda item: item["path"].casefold()):
        root = Path(declaration["path"])
        include_security = declaration["security_descriptors_requested"]
        rows = []

        def append_row(row, rows=rows):
            nonlocal serialized_row_bytes
            serialized_row_bytes += len(canonical_bytes(row)) + 1
            if serialized_row_bytes > 254 * 1024 * 1024:
                raise TimeoutError("complete acquisition exceeds artifact byte capacity")
            rows.append(row)

        def walk(path, root=root, rows=rows, include_security=include_security):
            nonlocal count, read_bytes
            budget()
            count += 1
            relative = path.relative_to(root).as_posix() if path != root else "."
            try:
                observed = path.lstat()
                directory = stat.S_ISDIR(observed.st_mode)
                with _held(path, directory) as (handle, info):
                    row = {"path": relative}
                    if include_security:
                        row.update(
                            descriptor_sha256=_security(handle),
                            mtime_ns=info.st_mtime_ns,
                            ctime_ns=info.st_ctime_ns,
                            attributes=info.st_file_attributes,
                        )
                    if directory:
                        append_row(dict(row, kind="directory"))
                        with os.scandir(path) as entries:
                            children = sorted(
                                (Path(entry.path) for entry in entries),
                                key=lambda item: item.name.casefold(),
                            )
                        for child in children:
                            walk(child)
                        with os.scandir(path) as entries:
                            after = sorted((entry.name for entry in entries), key=str.casefold)
                        if after != [child.name for child in children]:
                            raise ValueError("directory membership changed during capture")
                    else:
                        digest = hashlib.sha256()
                        actual = 0
                        for chunk in _chunks(handle):
                            actual += len(chunk)
                            read_bytes += len(chunk)
                            budget()
                            digest.update(chunk)
                        if actual != info.st_size:
                            raise ValueError("file length changed during capture")
                        append_row(
                            dict(row, kind="file", byte_length=actual, sha256=digest.hexdigest())
                        )
            except (OSError, ValueError, TimeoutError) as error:
                # Retain the failed row; never repair, skip silently, or report equality.
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
            if not rows:
                rows.append(
                    {"path": ".", "kind": "unreadable_object", "error_type": type(error).__name__}
                )
        result.append(
            {
                "path": str(root),
                "entries": rows,
                "file_count": sum(row["kind"] == "file" for row in rows),
                "unreadable_count": sum(row["kind"].startswith("unreadable") for row in rows),
                "security_descriptors_requested": include_security,
                "inventory_identity": content_identity(
                    "crash-protection-historical-root-inventory:v1", rows
                ),
            }
        )
    finished = _now()
    if hashlib.sha256(Path(__file__).read_bytes()).hexdigest() != collector_pin:
        errors.append({"collector": "implementation bytes changed during capture"})
    acquisition = {
        "contract_version": "policy-evidence-acquisition-v1",
        "fixture_only": request["fixture_only"],
        "origin": "NEW_OBSERVATION",
        "nonce": uuid.uuid4().hex,
        "started_utc": started_utc,
        "finished_utc": finished,
        "root_list_sha256": hashlib.sha256(canonical_bytes(request)).hexdigest(),
        "root_list": request,
        "method": "opened-windows-object-full-raw-inventory-v1",
        "collector_sha256": collector_pin,
        "runtime": {
            "implementation": sys.implementation.name,
            "version": sys.version,
            "executable_sha256": runtime_pin,
        },
        "raw_attribute_mask_ignored": 0,
        "complete": not errors,
        "observed_entries": count,
        "read_bytes": read_bytes,
        "errors": errors,
    }
    acquisition["observation_identity"] = content_identity(
        "policy-evidence-observation:v1", acquisition
    )
    body = {
        "phase": phase,
        "roots": result,
        "elapsed_seconds": time.monotonic() - started,
        "file_count": sum(root["file_count"] for root in result),
        "unreadable_count": sum(root["unreadable_count"] for root in result),
        "no_acl_write_or_recovery": True,
        "capture": acquisition,
    }
    body["identity"] = content_identity("crash-protection-historical-inventory:v1", body)
    if previous is not None:
        compared = compare_captures(previous, body)
        body.pop("identity")
        body.update(compared)
        if errors:
            body["equal"] = False
        body["identity"] = content_identity("crash-protection-historical-inventory:v1", body)
    _publish(output, canonical_bytes(body) + b"\n")
    return body


def validate_capture_provenance(value: dict) -> dict:
    """Verify the complete producer contract against this trusted deployment.

    Native human intake supplies authority. These mechanical checks prevent a
    differently implemented/fixture collector from silently becoming operational.
    They cannot replace the human's review of the actual observation.
    """
    capture = value.get("capture")
    required = {
        "contract_version",
        "fixture_only",
        "origin",
        "nonce",
        "started_utc",
        "finished_utc",
        "root_list_sha256",
        "root_list",
        "method",
        "collector_sha256",
        "runtime",
        "raw_attribute_mask_ignored",
        "complete",
        "observed_entries",
        "read_bytes",
        "errors",
        "observation_identity",
    }
    if not isinstance(capture, dict) or set(capture) != required:
        raise ValueError("complete versioned capture provenance is required")
    request = capture["root_list"]
    if (
        capture["contract_version"] != "policy-evidence-acquisition-v1"
        or capture["origin"] != "NEW_OBSERVATION"
        or type(capture["fixture_only"]) is not bool
        or capture["complete"] is not True
        or capture["errors"] != []
        or capture["raw_attribute_mask_ignored"] != 0
        or capture["method"] != "opened-windows-object-full-raw-inventory-v1"
        or not re.fullmatch(r"[0-9a-f]{32}", str(capture["nonce"]))
        or capture["collector_sha256"] != hashlib.sha256(Path(__file__).read_bytes()).hexdigest()
        or capture["runtime"]
        != {
            "implementation": sys.implementation.name,
            "version": sys.version,
            "executable_sha256": hashlib.sha256(Path(sys.executable).read_bytes()).hexdigest(),
        }
    ):
        raise ValueError("capture completeness/method/collector/runtime provenance mismatch")
    if (
        not isinstance(request, dict)
        or set(request) != {"contract_version", "fixture_only", "roots"}
        or (
            request["contract_version"] != "policy-evidence-root-list-v1"
            or request["fixture_only"] is not capture["fixture_only"]
            or capture["root_list_sha256"] != hashlib.sha256(canonical_bytes(request)).hexdigest()
            or sorted(request["roots"], key=lambda item: item["path"].casefold())
            != [
                {
                    "path": root["path"],
                    "security_descriptors_requested": root["security_descriptors_requested"],
                }
                for root in value["roots"]
            ]
        )
    ):
        raise ValueError("capture explicit root-list/coverage binding mismatch")
    if (
        type(capture["observed_entries"]) is not int
        or type(capture["read_bytes"]) is not int
        or capture["observed_entries"] != sum(len(root["entries"]) for root in value["roots"])
        or capture["read_bytes"]
        != sum(row.get("byte_length", 0) for root in value["roots"] for row in root["entries"])
        or utc(capture["finished_utc"]) < utc(capture["started_utc"])
        or capture["observation_identity"]
        != content_identity(
            "policy-evidence-observation:v1",
            {name: item for name, item in capture.items() if name != "observation_identity"},
        )
    ):
        raise ValueError("capture complete byte/entry/time/identity provenance mismatch")
    return capture


class ProductionInventory(Sealed):
    namespace = "production-python-modules:v1"
    contract_version: Literal["production-python-modules-v1"]
    candidate: Candidate
    repository_root: Text
    git_tool: dict
    commit_object_base64: str = Field(min_length=1, max_length=1024 * 1024)
    fixture_only: bool
    entries: tuple[dict, ...] = Field(min_length=1, max_length=100_000)
    physical_entries: tuple[dict, ...] = Field(min_length=1, max_length=100_000)
    production_modules: tuple[str, ...] = Field(min_length=1)
    inclusion_rule: Literal["complete-reviewed-git-tree-and-physical-production-python-v1"]
    started_utc: Text
    finished_utc: Text
    identity: Text

    @model_validator(mode="after")
    def _proof(self) -> Self:
        from tradingagents.windows_sandbox_runner import (
            SourceManifestEntry,
            _tree_hash_from_entries,
        )

        entries = tuple(SourceManifestEntry.model_validate(item) for item in self.entries)
        from tradingagents.policy_qualified_contract import BytePin

        if (
            set(self.git_tool) != {"path", "pin", "version"}
            or not Path(self.git_tool["path"]).is_absolute()
            or not isinstance(self.git_tool["version"], str)
            or not self.git_tool["version"].startswith("git version ")
        ):
            raise ValueError("explicit observed Git tool provenance required")
        BytePin.read(self.git_tool["pin"])

        if len(self.physical_entries) != len(entries):
            raise ValueError("physical/source manifest coverage mismatch")
        for physical, canonical in zip(self.physical_entries, entries, strict=True):
            if (
                set(physical) != {"relative_path", "pin", "checkout_form"}
                or physical["relative_path"] != canonical.relative_path
                or physical["checkout_form"] not in {"IDENTICAL", "UTF8_CRLF_CHECKOUT"}
            ):
                raise ValueError("physical source entry schema/order mismatch")
            pin = BytePin.read(physical["pin"])
            if physical["checkout_form"] == "IDENTICAL" and (
                pin.byte_length != canonical.byte_length or pin.sha256 != canonical.sha256
            ):
                raise ValueError("identical physical/Git bytes have different pins")
            if pin.byte_length < canonical.byte_length:
                raise ValueError("physical checkout is shorter than canonical Git bytes")
        if any(
            Path(entry.relative_path).is_absolute()
            or ".." in Path(entry.relative_path).parts
            or "\\" in entry.relative_path
            for entry in entries
        ):
            raise ValueError("source manifest path escapes its physical root")
        if tuple(entry.relative_path for entry in entries) != tuple(
            sorted({entry.relative_path for entry in entries})
        ):
            raise ValueError("source inventory is duplicated or unordered")
        if _tree_hash_from_entries(entries, object_format="sha1") != self.candidate.tree:
            raise ValueError("complete source tree proof mismatch")
        commit = base64.b64decode(self.commit_object_base64, validate=True)
        if (
            hashlib.sha1(b"commit " + str(len(commit)).encode("ascii") + b"\0" + commit).hexdigest()
            != self.candidate.commit
        ):
            raise ValueError("source commit proof mismatch")
        header = commit.split(b"\n\n", 1)[0].splitlines()
        if header[0] != b"tree " + self.candidate.tree.encode() or [
            row for row in header if row.startswith(b"parent ")
        ] != [b"parent " + self.candidate.parent.encode()]:
            raise ValueError("source parent/tree ancestry mismatch")
        modules = tuple(
            entry.relative_path
            for entry in entries
            if entry.relative_path.startswith(("tradingagents/", "cli/"))
            and entry.relative_path.endswith(".py")
        )
        if modules != self.production_modules:
            raise ValueError("production module coverage differs from complete source tree")
        if utc(self.finished_utc) < utc(self.started_utc):
            raise ValueError("source capture clock regressed")
        return self


def _git(repository: Path, executable: Path, *args: str) -> bytes:
    environment = {
        key: value for key, value in os.environ.items() if not key.upper().startswith("GIT_")
    }
    environment["GIT_TERMINAL_PROMPT"] = "0"
    result = subprocess.run(
        [
            str(executable),
            "-c",
            "safe.directory=" + str(repository),
            "-c",
            "core.fsmonitor=false",
            "-c",
            "core.hooksPath=NUL",
            *args,
        ],
        cwd=repository,
        env=environment,
        capture_output=True,
        check=True,
        timeout=30,
        creationflags=subprocess.CREATE_NO_WINDOW,
    )
    return result.stdout


def _physical_bytes(root: Path, relative: str) -> bytes:
    if Path(relative).is_absolute() or ".." in Path(relative).parts or "\\" in relative:
        raise ValueError("source path escapes declared root")
    path = root / relative
    with _ancestors(path), _held(path, False) as (handle, info):
        if info.st_size > 64 * 1024 * 1024:
            raise ValueError("individual source file exceeds inventory byte bound")
        raw = b"".join(_chunks(handle))
        if len(raw) != info.st_size:
            raise ValueError("source file changed during read")
        return raw


def validate_production_inventory(
    inventory: ProductionInventory, repository: Path | None = None
) -> None:
    """Independent consumer of a complete snapshot proof; optionally recheck physical bytes."""
    inventory = ProductionInventory.read(inventory)
    if repository is None:
        return
    repository = repository.absolute()
    from tradingagents.windows_sandbox_runner import _validate_input_content

    expected = set()
    for entry, physical in zip(inventory.entries, inventory.physical_entries, strict=True):
        relative = entry["relative_path"]
        raw = _physical_bytes(repository, relative)
        if (
            len(raw) != physical["pin"]["byte_length"]
            or hashlib.sha256(raw).hexdigest() != physical["pin"]["sha256"]
        ):
            raise ValueError(
                "physical source differs from independently verified manifest: " + relative
            )
        canonical = raw
        if physical["checkout_form"] == "UTF8_CRLF_CHECKOUT":
            raw.decode("utf-8")
            canonical = raw.replace(b"\r\n", b"\n")
        _validate_input_content(relative, canonical)
        if (
            len(canonical) != entry["byte_length"]
            or hashlib.sha256(canonical).hexdigest() != entry["sha256"]
            or hashlib.sha1(b"blob " + str(len(canonical)).encode() + b"\0" + canonical).hexdigest()
            != entry["git_object_id"]
        ):
            raise ValueError(
                "physical source differs from independently verified manifest: " + relative
            )
        expected.add(relative)
    for package in ("tradingagents", "cli"):
        base = repository / package
        if not base.exists():
            continue

        def visit(directory):
            with _held(directory, True):
                with os.scandir(directory) as stream:
                    paths = [Path(entry.path) for entry in stream]
                for path in paths:
                    info = path.lstat()
                    directory_entry = stat.S_ISDIR(info.st_mode)
                    _ordinary(path, directory_entry)
                    if directory_entry:
                        visit(path)
                    elif (
                        path.suffix.lower() in {".py", ".pyc", ".pyd", ".pth", ".so"}
                        and path.relative_to(repository).as_posix() not in expected
                    ):
                        raise ValueError("untracked executable/import input in production source")

        with _ancestors(base):
            visit(base)


def produce_production_inventory(
    repository: Path,
    output: Path,
    *,
    expected: Candidate,
    git_executable: Path,
    git_sha256: str,
    fixture_only: bool = False,
) -> ProductionInventory:
    """Use an explicit approved local Git input, never an ambient PATH tool.

    The Git installation is a declared external bootstrap prerequisite; the
    owner Python depot is not advertised as containing it. Complete tree and
    commit proofs are independently checked instead of trusting Git output.
    """
    git_executable = git_executable.absolute()
    if not re.fullmatch(r"[0-9a-f]{64}", git_sha256):
        raise ValueError("exact local Git executable SHA-256 required")
    # Existing Git for Windows installs may hardlink their exact tool executable.
    # Its opened object is hash-pinned and held deny-write/delete across every
    # alias through all invocations; evidence payloads never get this exception.
    with (
        _ancestors(git_executable),
        _held(git_executable, False, pinned_tool=True) as (handle, _info),
    ):
        raw = b"".join(_chunks(handle))
        if hashlib.sha256(raw).hexdigest() != git_sha256:
            raise ValueError("explicit Git executable pin mismatch")
        from tradingagents.policy_qualified_contract import BytePin

        git_tool = {
            "path": str(git_executable),
            "pin": BytePin.of(raw).model_dump(mode="json"),
            "version": _git(repository, git_executable, "--version").decode("utf-8").strip(),
        }
        return _produce_production_inventory(
            repository,
            output,
            expected=expected,
            fixture_only=fixture_only,
            git_executable=git_executable,
            git_tool=git_tool,
        )


def _produce_production_inventory(
    repository, output, *, expected, fixture_only, git_executable, git_tool
):
    repository = repository.absolute()
    if output.exists() or output.absolute().is_relative_to(repository):
        raise ValueError("source inventory output must be fresh and outside its source")
    started = _now()
    deadline = time.monotonic() + 600
    total = 0
    candidate = Candidate.read(expected)

    def git(*args):
        return _git(repository, git_executable, *args)

    actual = git("show", "-s", "--format=%H%n%T%n%P", "HEAD").decode("ascii").strip().splitlines()
    if actual != [candidate.commit, candidate.tree, candidate.parent] or git(
        "ls-files", "--others", "--exclude-standard", "-z"
    ):
        raise ValueError("source is not the exact clean candidate")
    git("diff", "--cached", "--quiet", "HEAD", "--")
    entries = []
    physical_entries = []
    from tradingagents.windows_sandbox_runner import SourceManifestEntry, _validate_input_content

    for row in git("ls-tree", "-r", "-z", "--full-tree", "HEAD").split(b"\0"):
        if not row:
            continue
        if time.monotonic() >= deadline or len(entries) >= 100_000 or total > 1024**3:
            raise TimeoutError("finite production inventory time/file/byte bound exceeded")
        metadata, name = row.split(b"\t", 1)
        mode, kind, object_id = metadata.split()
        relative = name.decode("utf-8")
        if (
            kind != b"blob"
            or mode not in {b"100644", b"100755"}
            or Path(relative).is_absolute()
            or ".." in Path(relative).parts
            or "\\" in relative
        ):
            raise ValueError("unsupported reviewed source entry")
        raw = _physical_bytes(repository, relative)
        total += len(raw)
        canonical = raw
        checkout = "IDENTICAL"
        if (
            hashlib.sha1(b"blob " + str(len(raw)).encode() + b"\0" + raw).hexdigest()
            != object_id.decode()
        ):
            # Preserve both byte identities. This is explicit Git/Windows text
            # checkout representation, never normalization of historical evidence.
            raw.decode("utf-8")
            canonical = raw.replace(b"\r\n", b"\n")
            if (
                hashlib.sha1(
                    b"blob " + str(len(canonical)).encode() + b"\0" + canonical
                ).hexdigest()
                != object_id.decode()
            ):
                raise ValueError("source worktree/blob disagreement")
            checkout = "UTF8_CRLF_CHECKOUT"
        _validate_input_content(relative, canonical)
        physical_entries.append(
            {
                "relative_path": relative,
                "pin": {"byte_length": len(raw), "sha256": hashlib.sha256(raw).hexdigest()},
                "checkout_form": checkout,
            }
        )
        entries.append(
            SourceManifestEntry(
                relative_path=relative,
                git_mode=mode.decode(),
                git_object_id=object_id.decode(),
                byte_length=len(canonical),
                sha256=hashlib.sha256(canonical).hexdigest(),
            ).model_dump(mode="json")
        )
    entries.sort(key=lambda entry: entry["relative_path"])
    physical_entries.sort(key=lambda entry: entry["relative_path"])
    inventory = ProductionInventory.seal(
        contract_version="production-python-modules-v1",
        candidate=candidate,
        fixture_only=fixture_only,
        repository_root=str(repository),
        git_tool=git_tool,
        commit_object_base64=base64.b64encode(git("cat-file", "commit", candidate.commit)).decode(),
        entries=entries,
        physical_entries=physical_entries,
        production_modules=[
            entry["relative_path"]
            for entry in entries
            if entry["relative_path"].startswith(("tradingagents/", "cli/"))
            and entry["relative_path"].endswith(".py")
        ],
        inclusion_rule="complete-reviewed-git-tree-and-physical-production-python-v1",
        started_utc=started,
        finished_utc=_now(),
    )
    validate_production_inventory(inventory, repository)
    if (
        git("ls-files", "--others", "--exclude-standard", "-z")
        or git("rev-parse", "HEAD").decode().strip() != candidate.commit
    ):
        raise ValueError("source changed during complete acquisition")
    git("diff", "--cached", "--quiet", "HEAD", "--")
    _publish(output, inventory.to_bytes())
    return inventory

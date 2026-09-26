"""Actual local disposable mount-point fixtures; no elevation or registered owner."""

import ctypes
import os
from pathlib import Path

import pytest

from tests.fixtures.policy_coverage_fixture import junction_buffer


def make_junction(path: Path, target: Path):
    from ctypes import wintypes

    from tradingagents.policy_evidence_acquisition import _file_api

    path.mkdir()
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
    handle = api.CreateFileW(str(path), 0x40000000, 0, None, 3, 0x02200000, None)
    if handle == ctypes.c_void_p(-1).value:
        raise ctypes.WinError(ctypes.get_last_error())
    raw = junction_buffer(str(target))
    buffer = ctypes.create_string_buffer(raw)
    count = wintypes.DWORD()
    try:
        if not api.DeviceIoControl(
            handle, 0x000900A4, buffer, len(raw), None, 0, ctypes.byref(count), None
        ):
            raise ctypes.WinError(ctypes.get_last_error())
    finally:
        api.CloseHandle(handle)


def test_native_link_object_capture_holds_and_binds_separately_measured_target(tmp_path):
    from tradingagents.policy_coverage_native import measure_junction

    root = tmp_path / "root"
    root.mkdir()
    target = root / "target"
    target.mkdir()
    link = root / "link"
    make_junction(link, target)
    record = measure_junction(root, link)
    assert record["tag"] == 0xA0000003
    assert record["target_relative_path"] == "target"
    assert record["target_object_identity"] != record["object_identity"]
    assert (
        record["object_identity"]["volume_serial"]
        == record["target_root_identity"]["volume_serial"]
    )
    assert record["descriptor_sha256"]


@pytest.mark.parametrize("case", ["outside", "same_prefix", "dangling", "cycle", "root_cycle"])
def test_native_junction_rejects_invalid_target_relationships(tmp_path, case):
    from tradingagents.policy_coverage_native import measure_junction

    root = tmp_path / "root"
    root.mkdir()
    link = root / "link"
    if case == "outside":
        target = tmp_path / "elsewhere"
        target.mkdir()
    elif case == "same_prefix":
        target = tmp_path / "root-suffix"
        target.mkdir()
    elif case == "dangling":
        target = root / "missing"
    elif case == "cycle":
        target = link
    else:
        target = root
    make_junction(link, target)
    with pytest.raises((ValueError, OSError)):
        measure_junction(root, link)


def native_fixture(tmp_path):
    import hashlib

    from tradingagents.policy_coverage_contract import (
        CoverageMeasurement,
        artifact_ref,
        build_scope,
    )
    from tradingagents.policy_qualified_contract import Candidate, canonical_bytes, content_identity

    root = tmp_path / "root"
    root.mkdir()
    gap = root / "gap"
    gap.mkdir()
    (gap / "newly-observed.txt").write_bytes(b"new")
    (root / "known.txt").write_bytes(b"abc")
    target = root / "target"
    target.mkdir()
    link = root / "link"
    make_junction(link, target)
    rows = [
        {"path": ".", "kind": "directory"},
        {"path": "gap", "kind": "directory"},
        {
            "path": "gap",
            "kind": "unreadable_directory",
            "attributes": gap.lstat().st_file_attributes,
            "mtime_ns": gap.lstat().st_mtime_ns,
        },
        {
            "path": "known.txt",
            "kind": "file",
            "byte_length": 3,
            "sha256": hashlib.sha256(b"abc").hexdigest(),
        },
        {
            "path": "link",
            "kind": "reparse",
            "size": link.lstat().st_size,
            "mtime_ns": link.lstat().st_mtime_ns,
        },
        {"path": "target", "kind": "directory"},
    ]
    original = {}
    for phase in ("before", "after"):
        r = {
            "path": str(root),
            "entries": rows,
            "file_count": 1,
            "unreadable_count": 1,
            "security_descriptors_requested": False,
            "inventory_identity": content_identity(
                "crash-protection-historical-root-inventory:v1", rows
            ),
        }
        body = {
            "phase": phase,
            "roots": [r],
            "file_count": 1,
            "unreadable_count": 1,
            "no_acl_write_or_recovery": True,
            "elapsed_seconds": 1.0,
        }
        body["identity"] = content_identity("crash-protection-historical-inventory:v1", body)
        original[phase] = canonical_bytes(body) + b"\n"
    scope = build_scope(original["before"], original["after"], fixture_only=True)
    m = CoverageMeasurement.seal(
        contract_version="policy-coverage-measurement-v2",
        scope=artifact_ref(scope),
        candidate=Candidate(commit="1" * 40, tree="2" * 40, parent="3" * 40),
        supported_reparse_tags=(0xA0000003,),
        security_required=True,
        maximum_entries=100,
        maximum_read_bytes=1024**2,
        maximum_seconds=30,
        maximum_depth=128,
        maximum_reparse_bytes=16384,
        maximum_indirect_link_depth=0,
        target_coverage="SEPARATE_ORDINARY_ROOT_WALK",
        known_fields_required=True,
    )
    return root, original, scope, m


def test_actual_disposable_complete_window_and_known_field_projection(tmp_path):
    from tradingagents.policy_coverage_contract import (
        compare_current_inventories,
        compare_known_fields,
    )
    from tradingagents.policy_coverage_native import capture_coverage

    root, old, scope, measurement = native_fixture(tmp_path)
    before = capture_coverage(
        scope,
        measurement,
        tmp_path / "before.json",
        instance="test-20990101-999",
        phase="before",
        fixture_only=True,
    )
    assert before["capture"]["complete"] is True
    assessment = compare_known_fields(scope, old["before"], old["after"], before, measurement)
    assert assessment.historical_coverage == "UNKNOWN"
    assert [x["path"] for x in assessment.expansions] == ["gap/newly-observed.txt"]
    after = capture_coverage(
        scope,
        measurement,
        tmp_path / "after.json",
        instance="test-20990101-999",
        phase="after",
        fixture_only=True,
    )
    assert compare_current_inventories(before, after, scope, measurement)["equal"] is True
    (root / "known.txt").write_bytes(b"changed")
    changed = capture_coverage(
        scope,
        measurement,
        tmp_path / "changed.json",
        instance="test-20990101-999",
        phase="after",
        fixture_only=True,
    )
    assert compare_current_inventories(before, changed, scope, measurement)["equal"] is False


def test_native_alias_ancestor_is_not_a_containment_path(tmp_path):
    from tradingagents.policy_coverage_native import measure_junction

    root = tmp_path / "root"
    root.mkdir()
    target = root / "target"
    target.mkdir()
    alias = root / "alias"
    make_junction(alias, target)
    actual = target / "nested"
    make_junction(actual, target)
    with pytest.raises(ValueError, match="aliased|reparse"):
        measure_junction(root, alias / "nested")


def protect_same_disposable_dacl(path):
    """Change only this new fixture's DACL inheritance bit; preserve its ACEs."""
    from ctypes import wintypes

    api = ctypes.WinDLL("advapi32", use_last_error=True)
    kernel = ctypes.WinDLL("kernel32")
    get = api.GetNamedSecurityInfoW
    get.argtypes = [
        wintypes.LPWSTR,
        ctypes.c_int,
        wintypes.DWORD,
        ctypes.c_void_p,
        ctypes.c_void_p,
        ctypes.POINTER(ctypes.c_void_p),
        ctypes.c_void_p,
        ctypes.POINTER(ctypes.c_void_p),
    ]
    get.restype = wintypes.DWORD
    set_info = api.SetNamedSecurityInfoW
    set_info.argtypes = [
        wintypes.LPWSTR,
        ctypes.c_int,
        wintypes.DWORD,
        ctypes.c_void_p,
        ctypes.c_void_p,
        ctypes.c_void_p,
        ctypes.c_void_p,
    ]
    set_info.restype = wintypes.DWORD
    kernel.LocalFree.argtypes = [ctypes.c_void_p]
    dacl = ctypes.c_void_p()
    descriptor = ctypes.c_void_p()
    status = get(str(path), 1, 4, None, None, ctypes.byref(dacl), None, ctypes.byref(descriptor))
    if status:
        raise ctypes.WinError(status)
    try:
        status = set_info(str(path), 1, 0x80000004, None, None, dacl, None)
        if status:
            raise ctypes.WinError(status)
    finally:
        kernel.LocalFree(descriptor)


def test_ordinary_native_measurement_rejects_descriptor_change(tmp_path):
    from tradingagents.policy_coverage_native import _held

    path = tmp_path / "disposable-security-subject.txt"
    path.write_bytes(b"unchanged bytes")
    with pytest.raises(ValueError, match="security|descriptor"), _held(path, False):
        protect_same_disposable_dacl(path)


def test_complete_native_inventory_rejects_mutual_subtree_link_cycle(tmp_path):
    import json

    from tradingagents.policy_coverage_contract import (
        CoverageMeasurement,
        artifact_ref,
        build_scope,
    )
    from tradingagents.policy_coverage_native import capture_coverage
    from tradingagents.policy_qualified_contract import canonical_bytes, content_identity

    root, old, _, measurement = native_fixture(tmp_path)
    a = root / "a"
    a.mkdir()
    b = root / "b"
    b.mkdir()
    make_junction(a / "to-b", b)
    make_junction(b / "to-a", a)
    revised = {}
    for phase, raw in old.items():
        value = json.loads(raw)
        r = value["roots"][0]
        for name, path, kind in [
            ("a", a, "directory"),
            ("a/to-b", a / "to-b", "reparse"),
            ("b", b, "directory"),
            ("b/to-a", b / "to-a", "reparse"),
        ]:
            row = {"path": name, "kind": kind}
            if kind == "reparse":
                row.update(size=path.lstat().st_size, mtime_ns=path.lstat().st_mtime_ns)
            r["entries"].append(row)
        r["entries"].sort(key=lambda x: (x["path"].casefold(), x["kind"]))
        r["inventory_identity"] = content_identity(
            "crash-protection-historical-root-inventory:v1", r["entries"]
        )
        value.pop("identity")
        value["identity"] = content_identity("crash-protection-historical-inventory:v1", value)
        revised[phase] = canonical_bytes(value) + b"\n"
    scope = build_scope(revised["before"], revised["after"], fixture_only=True)
    payload = measurement.model_dump(mode="json")
    payload.pop("identity")
    payload["scope"] = artifact_ref(scope)
    measurement = CoverageMeasurement.seal(**payload)
    value = capture_coverage(
        scope,
        measurement,
        tmp_path / "mutual-cycle.json",
        instance="test-20990101-999",
        phase="before",
        fixture_only=True,
    )
    assert value["capture"]["complete"] is False
    assert any("cycle" in str(error) for error in value["capture"]["errors"])


def test_opened_native_objects_reject_substitution_attempt(tmp_path):
    from tradingagents.policy_coverage_native import _held, _link_object

    root = tmp_path / "root"
    root.mkdir()
    target = root / "target"
    target.mkdir()
    link = root / "link"
    make_junction(link, target)
    with _held(root, True), pytest.raises(OSError):
        os.rename(root, tmp_path / "replaced-root")
    with _link_object(link), pytest.raises(OSError):
        os.rename(link, root / "replaced-link")
    assert root.is_dir() and link.lstat().st_file_attributes & 0x400


def test_native_wide_directory_enumeration_stops_at_declared_entry_budget(tmp_path, monkeypatch):
    from tradingagents.policy_coverage_contract import CoverageMeasurement
    from tradingagents.policy_coverage_native import capture_coverage

    root, old, scope, measurement = native_fixture(tmp_path)
    for index in range(100):
        (root / f"wide-{index:03}.txt").write_bytes(b"bounded fixture")
    values = measurement.model_dump(mode="json", exclude={"identity"})
    values["maximum_entries"] = 3
    measurement = CoverageMeasurement.seal(**values)
    original = os.scandir
    observed = []

    class CountActualEntries:
        def __init__(self, path):
            self.path = Path(path)
            self.inner = original(path)

        def __enter__(self):
            self.inner.__enter__()
            return self

        def __exit__(self, *args):
            return self.inner.__exit__(*args)

        def __iter__(self):
            return self

        def __next__(self):
            entry = next(self.inner)
            if self.path == root:
                observed.append(entry.name)
            return entry

    with monkeypatch.context() as context:
        context.setattr(os, "scandir", CountActualEntries)
        result = capture_coverage(
            scope,
            measurement,
            tmp_path / "bounded-wide.json",
            instance="test-20990101-999",
            phase="before",
            fixture_only=True,
        )
    assert result["capture"]["complete"] is False
    assert len(observed) <= 4


def test_exhausted_time_budget_prevents_even_first_root_ancestor_open(tmp_path, monkeypatch):
    from contextlib import contextmanager

    from tradingagents import policy_coverage_native as native
    from tradingagents.policy_coverage_contract import CoverageMeasurement

    root, old, scope, measurement = native_fixture(tmp_path)
    body = measurement.model_dump(mode="json", exclude={"identity"})
    body["maximum_seconds"] = 1
    measurement = CoverageMeasurement.seal(**body)
    opened = []
    original = native._ancestors
    clock_calls = []

    @contextmanager
    def observed(path):
        opened.append(path)
        with original(path):
            yield

    def fixture_clock():
        clock_calls.append(True)
        return 0 if len(clock_calls) == 1 else 2

    with monkeypatch.context() as context:
        context.setattr(native, "_ancestors", observed)
        context.setattr(native.time, "monotonic", fixture_clock)
        value = native.capture_coverage(
            scope,
            measurement,
            tmp_path / "timed-out.json",
            instance="test-20990101-999",
            phase="before",
            fixture_only=True,
        )
    assert opened == []
    assert value["capture"]["complete"] is False and value["capture"]["read_bytes"] == 0
    assert value["capture"]["errors"][0]["remaining_roots_not_visited"] == 1

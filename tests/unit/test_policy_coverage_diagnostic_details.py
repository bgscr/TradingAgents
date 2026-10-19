"""Pure synthetic guard/projection tests; no native acquisition or child process."""

import io
import json
import stat
from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from tests.fixtures.policy_coverage_fixture import make_coverage_fixture
from tests.policy_coverage_diagnostics import capture_failure_diagnostic
from tradingagents import policy_coverage_native as native


@pytest.fixture(autouse=True)
def forbid_native(monkeypatch):
    def forbidden(*args, **kwargs):
        raise AssertionError("native boundary must be replaced by synthetic inputs")

    for name in ("_file_api", "_available_local", "_security", "_link_object", "_chunks",
                 "new_evidence_output"):
        monkeypatch.setattr(native, name, forbidden)


def snapshot(**changes):
    values = {"st_dev": 1, "st_ino": 2, "st_size": 3, "st_mtime_ns": 4,
              "st_ctime_ns": 5, "st_file_attributes": 0, "st_mode": stat.S_IFREG,
              "st_nlink": 1}
    values.update(changes)
    return SimpleNamespace(**values)


def opened(monkeypatch, *, after=None, identities=None, security=None):
    before = snapshot()
    available = Mock(side_effect=[before, after if after is not None else snapshot()])
    identity = Mock(side_effect=identities or [
        {"volume_serial": "PRIVATE_VOLUME", "file_index": "PRIVATE_INDEX"},
        {"volume_serial": "PRIVATE_VOLUME", "file_index": "PRIVATE_INDEX"},
    ])
    descriptors = Mock(side_effect=security or ["PRIVATE_SECURITY", "PRIVATE_SECURITY"])
    api = SimpleNamespace(CreateFileW=Mock(return_value=42), CloseHandle=Mock())
    monkeypatch.setattr(native, "_available_local", available)
    monkeypatch.setattr(native, "_file_api", lambda: (api, None))
    monkeypatch.setattr(native, "_information", lambda _: SimpleNamespace(
        attributes=0, links=1, index_high=0, index_low=2))
    monkeypatch.setattr(native, "_identity", identity)
    monkeypatch.setattr(native, "_security", descriptors)
    return api, available, identity, descriptors


@pytest.mark.parametrize("field", native._HELD_FIELDS)
def test_each_changed_field_rejects_with_labels_only(monkeypatch, field):
    after = snapshot()
    identities = [
        {"volume_serial": "PRIVATE_VOLUME", "file_index": "PRIVATE_INDEX"},
        {"volume_serial": "PRIVATE_VOLUME", "file_index": "PRIVATE_INDEX"},
    ]
    if field.startswith("handle_"):
        identities[1][field.removeprefix("handle_")] = "PRIVATE_CHANGED"
    else:
        setattr(after, field, 0x10000000 if field == "st_file_attributes" else 999999999)
    api, available, identity, security = opened(monkeypatch, after=after, identities=identities)
    with pytest.raises(ValueError) as failure, native._held(Path("PRIVATE_PATH"), False):
        pass
    error = failure.value
    assert type(error) is native._HeldMeasurementChanged
    assert str(error) == "held measurement object changed during acquisition"
    details = native._held_error_details(error)
    assert details == {"changed_fields": [field], "object_role": "ordinary_file"}
    assert "PRIVATE" not in json.dumps(details)
    assert "999999999" not in json.dumps(details)
    assert available.call_count == identity.call_count == security.call_count == 2
    api.CreateFileW.assert_called_once_with("PRIVATE_PATH", 0x80000000, 1, None, 3, 0x02300000, None)
    api.CloseHandle.assert_called_once_with(42)


def test_unchanged_snapshot_passes_without_extra_queries(monkeypatch):
    api, available, identity, security = opened(monkeypatch)
    with native._held(Path("PRIVATE_PATH"), False):
        pass
    assert available.call_count == identity.call_count == security.call_count == 2
    api.CloseHandle.assert_called_once_with(42)


def test_identity_change_keeps_metadata_short_circuit(monkeypatch):
    class UnreadableMetadata:
        def __getattr__(self, name):
            raise AssertionError("metadata must remain unevaluated")

    opened(monkeypatch, after=UnreadableMetadata(), identities=[
        {"volume_serial": "a", "file_index": "b"},
        {"volume_serial": "c", "file_index": "b"},
    ])
    with (
        pytest.raises(native._HeldMeasurementChanged) as failure,
        native._held(Path("PRIVATE_PATH"), False),
    ):
        pass
    assert failure.value.changed_fields == ("handle_volume_serial",)


def test_security_failure_still_precedes_identity_check(monkeypatch):
    api, _available, identity, _security = opened(monkeypatch, security=["old", "new"])
    with (
        pytest.raises(ValueError, match="security descriptor changed") as failure,
        native._held(Path("PRIVATE_PATH"), False),
    ):
        pass
    assert native._held_error_details(failure.value) == {}
    assert identity.call_count == 1
    api.CloseHandle.assert_called_once_with(42)


def test_body_exception_is_not_replaced_and_handle_closes(monkeypatch):
    api, available, identity, security = opened(monkeypatch)
    original = RuntimeError("synthetic body failure")
    with pytest.raises(RuntimeError) as failure, native._held(Path("PRIVATE_PATH"), False):
        raise original
    assert failure.value is original
    assert available.call_count == identity.call_count == security.call_count == 1
    api.CloseHandle.assert_called_once_with(42)


def test_multiple_metadata_differences_are_preserved(monkeypatch):
    opened(monkeypatch, after=snapshot(st_size=99, st_ctime_ns=88, st_file_attributes=0x10000000))
    with (
        pytest.raises(native._HeldMeasurementChanged) as failure,
        native._held(Path("PRIVATE_PATH"), False),
    ):
        pass
    assert failure.value.changed_fields == ("st_size", "st_ctime_ns", "st_file_attributes")


@pytest.mark.parametrize("role", sorted(native._HELD_ROLES))
def test_projection_accepts_only_fixed_role_labels(role):
    error = native._HeldMeasurementChanged(("st_mtime_ns",), role)
    result = json.loads(capture_failure_diagnostic([
        {"path": "PRIVATE_PATH", "error": str(error), **native._held_error_details(error)}
    ]))
    assert result["errors"][0]["object_role"] == role
    assert result["errors"][0]["changed_fields"] == ["st_mtime_ns"]
    assert "PRIVATE" not in json.dumps(result)


@pytest.mark.parametrize("details", [
    {"changed_fields": ["PRIVATE_FIELD"], "object_role": "ordinary_file"},
    {"changed_fields": ["st_size"], "object_role": "PRIVATE_ROLE"},
    {"changed_fields": ["st_size"] * 9, "object_role": "ordinary_file"},
    {"changed_fields": ["st_size", "st_size"], "object_role": "ordinary_file"},
    {"changed_fields": [], "object_role": "ordinary_file"},
    {"changed_fields": "st_size", "object_role": "ordinary_file"},
    {"changed_fields": [123], "object_role": "ordinary_file"},
])
def test_invalid_details_are_dropped_without_changing_failure(details):
    result = json.loads(capture_failure_diagnostic([{
        "path": "PRIVATE_PATH", "error": "held measurement object changed during acquisition",
        "raw_identity": "PRIVATE_IDENTITY", **details,
    }]))["errors"][0]
    assert result == {"stage": "entry", "message_kind": "known_guard",
                      "guard": "held measurement object changed during acquisition"}


def test_details_on_other_errors_are_not_admitted():
    result = json.loads(capture_failure_diagnostic([{
        "error": "directory membership changed during capture",
        "changed_fields": ["st_size"], "object_role": "ordinary_file",
    }]))["errors"][0]
    assert "changed_fields" not in result
    assert "object_role" not in result


def synthetic_capture(monkeypatch, tmp_path, failing_role, *, junction=False):
    fixture = make_coverage_fixture()
    root = Path(fixture["scope"].roots[0])
    observed_roles = []

    @contextmanager
    def held(path, directory, *, role=None):
        actual_role = role or ("ordinary_directory" if directory else "ordinary_file")
        observed_roles.append(actual_role)
        yield str(path), snapshot(), "PRIVATE_SECURITY"
        if actual_role == failing_role:
            raise native._HeldMeasurementChanged(("st_file_attributes",), actual_role)

    def available(path):
        if junction and path == root / "link":
            return snapshot(st_file_attributes=0x400, st_mode=stat.S_IFDIR)
        return snapshot(st_file_attributes=16, st_mode=stat.S_IFDIR)

    @contextmanager
    def entries(path):
        yield [SimpleNamespace(path=str(root / "link"))] if junction and path == root else []

    @contextmanager
    def link(path):
        yield "link", snapshot(), {"object_identity": {
            "volume_serial": "v", "file_index": "link"}}, b"SYNTHETIC"

    output = io.BytesIO()

    @contextmanager
    def publish(path):
        yield output

    monkeypatch.setattr(native, "_held", held)
    monkeypatch.setattr(native, "_available_local", available)
    monkeypatch.setattr(native.os, "scandir", entries)
    monkeypatch.setattr(native, "_metadata", lambda *args: {})
    monkeypatch.setattr(native, "_link_object", link)
    monkeypatch.setattr(native, "parse_mount_point", lambda _: str(root / "target"))
    monkeypatch.setattr(native, "_identity", lambda handle: {
        "volume_serial": "v", "file_index": str(handle)})
    monkeypatch.setattr(native, "new_evidence_output", publish)
    value = native.capture_coverage(
        fixture["scope"], fixture["measurement"], tmp_path / "synthetic.json",
        instance="test-20990101-999", phase="before", fixture_only=True,
    )
    assert json.loads(output.getvalue()) == value
    assert value["capture"]["complete"] is False
    safe = json.loads(capture_failure_diagnostic(value["capture"]["errors"]))
    assert "PRIVATE" not in json.dumps(safe)
    return safe["errors"], observed_roles


@pytest.mark.parametrize("role,junction,stage", [
    ("ordinary_directory", False, "entry"),
    ("ancestor", False, "root_or_ancestor"),
    ("junction_root", True, "entry"),
    ("junction_target", True, "entry"),
    ("ancestor", True, "entry"),
])
def test_actual_capture_propagates_held_role_even_for_nested_ancestor(
    monkeypatch, tmp_path, role, junction, stage,
):
    errors, observed = synthetic_capture(monkeypatch, tmp_path, role, junction=junction)
    assert role in observed
    assert any(item.get("object_role") == role and item["stage"] == stage for item in errors)
    assert all(item.get("changed_fields") == ["st_file_attributes"] for item in errors)


def test_parent_chain_tags_its_held_objects(monkeypatch, tmp_path):
    from contextlib import ExitStack

    roles = []

    @contextmanager
    def held(path, directory, *, role=None):
        roles.append(role)
        yield None
        raise native._HeldMeasurementChanged(("st_size",), role)

    monkeypatch.setattr(native, "_held", held)
    with pytest.raises(native._HeldMeasurementChanged) as failure, ExitStack() as stack:
        native._hold_chain(stack, tmp_path, tmp_path / "nested")
    assert roles == ["junction_parent"]
    assert failure.value.object_role == "junction_parent"

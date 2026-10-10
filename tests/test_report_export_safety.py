"""Pure report regressions; links and Windows handle sharing are simulated."""

import copy
import ctypes
import os
import stat
import sys
from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace

import pytest

from tests.report_export_helpers import export_manifest, export_root
from tests.test_reporting import _decision_state, _outcome_state
from tradingagents import report_storage
from tradingagents.decision_audit import write_immutable_decision_audit
from tradingagents.evidence import (
    AnalysisDiagnosticCode,
    AnalysisOutcomeReason,
)
from tradingagents.report_exports import write_advisory_draft
from tradingagents.report_presentation import _BLOCKER_ZH, _OUTCOME_ZH
from tradingagents.reporting import write_report_tree


@pytest.fixture(autouse=True)
def model_windows_handles(monkeypatch):
    initialize = report_storage.ReportStore.__init__

    def initialize_windows_model(self, root):
        initialize(self, root)
        self._windows = True

    @contextmanager
    def hold(path, *, directory):
        report_storage._ordinary(path.lstat(), directory=directory)
        yield
        report_storage._ordinary(path.lstat(), directory=directory)

    monkeypatch.setattr(report_storage, "_hold_windows_object", hold)
    monkeypatch.setattr(report_storage.ReportStore, "__init__", initialize_windows_model)

    def create(path):
        descriptor = os.open(
            path, os.O_RDWR | os.O_CREAT | os.O_EXCL | getattr(os, "O_BINARY", 0), 0o600
        )
        return os.fdopen(descriptor, "w+b", buffering=0)

    monkeypatch.setattr(report_storage, "_create_windows_file", create)


def simulate_link(monkeypatch, path, *, reparse):
    original = Path.lstat

    def lstat(value, *args, **kwargs):
        info = original(value, *args, **kwargs)
        if value == path:
            return SimpleNamespace(
                st_mode=info.st_mode if reparse else stat.S_IFLNK,
                st_file_attributes=0x400 if reparse else 0,
                st_nlink=info.st_nlink,
            )
        return info

    monkeypatch.setattr(Path, "lstat", lstat)


@pytest.mark.parametrize("location", ("ancestor", "root", "directory", "file", "entrypoint"))
@pytest.mark.parametrize("reparse", (False, True), ids=("symlink-model", "junction-model"))
def test_report_rejects_simulated_links_before_publishing(tmp_path, monkeypatch, location, reparse):
    root = tmp_path / "parent" / "reports"
    root.mkdir(parents=True)
    outside = tmp_path / "outside.txt"
    outside.write_bytes(b"EXTERNAL TARGET MUST STAY UNCHANGED")
    if location in {"ancestor", "root"}:
        link = root.parent if location == "ancestor" else root
    elif location == "entrypoint":
        link = root / "complete_report.md"
        link.write_bytes(b"legacy entrypoint")
    else:
        directory = root / "1_analysts"
        directory.mkdir()
        link = directory
        if location == "file":
            link = directory / "market.md"
            link.write_bytes(b"legacy file")
    simulate_link(monkeypatch, link, reparse=reparse)
    with pytest.raises(ValueError, match="link or reparse"):
        write_report_tree(_decision_state(), "AAPL", root)
    assert outside.read_bytes() == b"EXTERNAL TARGET MUST STAY UNCHANGED"
    assert not (root / "decision-audit.json").exists()


@pytest.mark.parametrize("target_kind", ("directory", "file"))
def test_direct_target_link_model_keeps_outside_unchanged(tmp_path, monkeypatch, target_kind):
    outside = tmp_path / "outside.txt"
    outside.write_bytes(b"outside")
    directory = tmp_path / "version"
    directory.mkdir()
    file = directory / "stage.md"
    file.write_bytes(b"original")
    target = directory if target_kind == "directory" else file
    simulate_link(monkeypatch, target, reparse=True)
    with (
        pytest.raises(ValueError, match="link or reparse"),
        report_storage.ReportStore(tmp_path) as store,
    ):
        store.write_bytes("version/stage.md", b"new")
    assert file.read_bytes() == b"original"
    assert outside.read_bytes() == b"outside"


def test_equal_precreated_new_file_is_a_conflict(tmp_path):
    target = tmp_path / "version" / "stage.md"
    target.parent.mkdir()
    target.write_bytes(b"EXPECTED")
    with (
        report_storage.ReportStore(tmp_path) as store,
        pytest.raises(FileExistsError, match="already exists"),
    ):
        store.write_bytes("version/stage.md", b"EXPECTED")
    assert target.read_bytes() == b"EXPECTED"


def test_existing_immutable_input_is_held_until_export_closes(tmp_path, monkeypatch):
    target = tmp_path / "audit.json"
    target.write_bytes(b"IMMUTABLE")
    hold = report_storage._hold_windows_object
    active = set()

    @contextmanager
    def observed(path, *, directory):
        with hold(path, directory=directory):
            active.add(path)
            try:
                yield
            finally:
                active.remove(path)

    monkeypatch.setattr(report_storage, "_hold_windows_object", observed)
    with report_storage.ReportStore(tmp_path) as store:
        assert store.read_bytes("audit.json") == b"IMMUTABLE"
        assert target in active
        stream = store._created[0][1]
        assert not stream.closed
        store.write_bytes("version/report.md", b"NEW REPORT")
        assert target in active
    assert not active and stream.closed


def test_existing_input_interference_is_detected_at_final_verification(tmp_path):
    target = tmp_path / "audit.json"
    target.write_bytes(b"EXPECTED")
    with (
        pytest.raises(ValueError, match="content changed"),
        report_storage.ReportStore(tmp_path) as store,
    ):
        assert store.read_bytes("audit.json") == b"EXPECTED"
        # Pure model permits this write; real Windows sharing must deny it.
        target.write_bytes(b"MODIFIED")


def test_read_of_just_created_file_reuses_its_write_descriptor(tmp_path, monkeypatch):
    hold = report_storage._hold_windows_object

    def directories_only(path, *, directory):
        assert directory, "do not reopen a write-held file with incompatible sharing"
        return hold(path, directory=directory)

    monkeypatch.setattr(report_storage, "_hold_windows_object", directories_only)
    with report_storage.ReportStore(tmp_path) as store:
        store.write_bytes("audit.json", b"EXPECTED")
        assert store.read_bytes(tmp_path / "audit.json") == b"EXPECTED"
        assert len(store._created) == 1


def test_missing_stage_creates_exact_current_manifest_and_preserves_history(tmp_path):
    legacy = tmp_path / "1_analysts/news.md"
    legacy.parent.mkdir()
    legacy.write_bytes(b"UNVERSIONED LEGACY NEWS")
    root_alias = tmp_path / "news_report.md"
    root_alias.write_bytes(b"UNVERSIONED ROOT ALIAS")
    unknown = tmp_path / "user-notes.txt"
    unknown.write_bytes(b"USER NOTES")
    state = _decision_state()
    state["news_report"] = "CURRENT NEWS"
    write_report_tree(state, "AAPL", tmp_path)
    first = export_root(tmp_path)
    first_manifest = export_manifest(tmp_path)
    audit = (tmp_path / "decision-audit.json").read_bytes()
    state.pop("news_report")
    entry = write_report_tree(state, "AAPL", tmp_path)
    current = export_root(entry)
    manifest = export_manifest(entry)
    assert current != first
    assert "1_analysts/news.md" in first_manifest["advisory_stages"]
    assert "1_analysts/news.md" not in manifest["advisory_stages"]
    assert not (current / "1_analysts/news.md").exists()
    assert "CURRENT NEWS" in (first / "1_analysts/news.md").read_text(encoding="utf-8")
    assert "CURRENT NEWS" not in entry.read_text(encoding="utf-8")
    assert manifest["run_id"] == first_manifest["run_id"] == state["run_id"]
    assert manifest["audit_sha256"] == first_manifest["audit_sha256"]
    assert (tmp_path / "decision-audit.json").read_bytes() == audit
    assert manifest["unversioned_root_paths"]["disposition"] == "historical-not-current"
    assert legacy.read_bytes() == b"UNVERSIONED LEGACY NEWS"
    assert root_alias.read_bytes() == b"UNVERSIONED ROOT ALIAS"
    assert unknown.read_bytes() == b"USER NOTES"


def test_changed_advisory_and_identical_reexport_both_keep_previous_versions(tmp_path):
    state = _decision_state()
    write_report_tree(state, "AAPL", tmp_path)
    first = export_root(tmp_path)
    audit = (tmp_path / "decision-audit.json").read_bytes()
    state["market_report"] = "REVISED ADVISORY ONLY"
    entry = write_report_tree(state, "AAPL", tmp_path)
    second = export_root(entry)
    second_bytes = entry.read_bytes()
    assert second != first
    assert "REVISED ADVISORY ONLY" not in (first / "1_analysts/market.md").read_text(
        encoding="utf-8"
    )
    assert "REVISED ADVISORY ONLY" in (second / "1_analysts/market.md").read_text(encoding="utf-8")
    assert (tmp_path / "decision-audit.json").read_bytes() == audit
    repeated = write_report_tree(state, "AAPL", tmp_path)
    assert export_root(repeated) != second
    assert export_manifest(repeated)["content_id"] == export_manifest(entry)["content_id"]
    assert entry.read_bytes() == second_bytes
    assert len(list((tmp_path / "report_exports").glob("*/manifest.json"))) == 3


def test_incomplete_version_never_replaces_current_entrypoint(tmp_path, monkeypatch):
    state = _decision_state()
    entry = write_report_tree(state, "AAPL", tmp_path)
    previous = entry.read_bytes()
    previous_id = export_manifest(tmp_path)["export_id"]
    state["market_report"] = "NEW CONTENT"
    original = report_storage.ReportStore.write_bytes

    def interrupted(store, relative, data, **kwargs):
        if str(relative).endswith("/manifest.json"):
            raise OSError("simulated publication interruption")
        return original(store, relative, data, **kwargs)

    monkeypatch.setattr(report_storage.ReportStore, "write_bytes", interrupted)
    with pytest.raises(OSError, match="simulated"):
        write_report_tree(state, "AAPL", tmp_path)
    assert entry.read_bytes() == previous
    assert export_manifest(entry)["export_id"] == previous_id


def test_unversioned_complete_report_and_streaming_alias_are_preserved(tmp_path):
    legacy = tmp_path / "complete_report.md"
    legacy.write_bytes(b"OLD COMPLETE REPORT")
    alias = tmp_path / "market_report.md"
    alias.write_bytes(b"OLD MARKET ALIAS")
    draft = write_advisory_draft(tmp_path, "market_report.md", "new draft")
    assert draft.read_text() == "new draft"
    assert alias.read_bytes() == b"OLD MARKET ALIAS"
    write_report_tree(_decision_state(), "AAPL", tmp_path)
    assert legacy.read_bytes() == b"OLD COMPLETE REPORT"
    assert alias.read_bytes() == b"OLD MARKET ALIAS"


def test_audit_cannot_publish_outside_its_pinned_report_root(tmp_path):
    outside = tmp_path / "must-not-be-created"
    with (
        report_storage.ReportStore(tmp_path / "reports") as store,
        pytest.raises(ValueError, match="pinned report root"),
    ):
        write_immutable_decision_audit(_decision_state(), outside, publication=store)
    assert not outside.exists()


def test_manual_entrypoint_edits_are_retained_even_with_an_export_marker(tmp_path):
    state = _decision_state()
    entry = write_report_tree(state, "AAPL", tmp_path)
    manual = entry.read_bytes() + b"\nMY MANUAL NOTES\n"
    entry.write_bytes(manual)
    write_report_tree(state, "AAPL", tmp_path)
    assert entry.read_bytes() == manual


@pytest.mark.parametrize("phase", ("after_flush", "before_close"))
def test_named_object_substitution_is_rejected_while_original_descriptor_is_open(
    tmp_path, monkeypatch, phase
):
    outside = tmp_path / "outside.txt"
    outside.write_bytes(b"OUTSIDE")
    original = report_storage.ReportStore._verify_created
    checks = []

    def interleaved(store, relative, stream, identity, digest, length):
        assert not stream.closed
        checks.append(relative)
        trigger = 1 if phase == "after_flush" else 2
        if len(checks) == trigger:
            inspect = store.inspect

            def substituted(path):
                info = inspect(path)
                return SimpleNamespace(st_dev=info.st_dev, st_ino=info.st_ino + 1)

            store.inspect = substituted
        return original(store, relative, stream, identity, digest, length)

    monkeypatch.setattr(report_storage.ReportStore, "_verify_created", interleaved)
    with (
        pytest.raises(ValueError, match="object changed"),
        report_storage.ReportStore(tmp_path) as store,
    ):
        store.write_bytes("new-report.md", b"EXPECTED")
    assert len(checks) == (1 if phase == "after_flush" else 2)
    assert outside.read_bytes() == b"OUTSIDE"


def test_content_interference_after_flush_is_rejected_before_close(tmp_path, monkeypatch):
    original = report_storage.ReportStore._verify_created

    def corrupt(store, relative, stream, identity, digest, length):
        assert not stream.closed
        stream.seek(0)
        stream.write(b"CORRUPT!")
        stream.flush()
        return original(store, relative, stream, identity, digest, length)

    monkeypatch.setattr(report_storage.ReportStore, "_verify_created", corrupt)
    with (
        pytest.raises(ValueError, match="content changed"),
        report_storage.ReportStore(tmp_path) as store,
    ):
        store.write_bytes("new-report.md", b"EXPECTED")


def test_no_closed_source_path_is_renamed_or_linked_to_publish(tmp_path, monkeypatch):
    def forbidden(*args, **kwargs):
        pytest.fail("publication must not rename/link a closed source path")

    monkeypatch.setattr(os, "replace", forbidden)
    monkeypatch.setattr(os, "link", forbidden)
    with report_storage.ReportStore(tmp_path) as store:
        store.write_bytes("new-report.md", b"EXPECTED")
        stream = store._created[0][1]
        assert not stream.closed
    assert stream.closed
    assert (tmp_path / "new-report.md").read_bytes() == b"EXPECTED"


@pytest.mark.parametrize("edit_at", ("after_alias_inspection", "before_manifest"))
def test_concurrent_old_alias_edit_is_neither_read_as_content_nor_replaced(
    tmp_path, monkeypatch, edit_at
):
    alias = tmp_path / "complete_report.md"
    alias.write_bytes(b"OLD ALIAS")
    edited = b"USER EDIT DURING EXPORT"
    inspect = report_storage.ReportStore.inspect
    write = report_storage.ReportStore.write_bytes
    read = report_storage.ReportStore.read_bytes

    def observe(store, relative):
        info = inspect(store, relative)
        if str(relative) == "complete_report.md" and edit_at == "after_alias_inspection":
            alias.write_bytes(edited)
        return info

    def publish(store, relative, data):
        if str(relative).endswith("/manifest.json") and edit_at == "before_manifest":
            alias.write_bytes(edited)
        return write(store, relative, data)

    def no_alias_read(store, relative):
        assert str(relative) != "complete_report.md", "legacy alias content must not be read"
        return read(store, relative)

    monkeypatch.setattr(report_storage.ReportStore, "inspect", observe)
    monkeypatch.setattr(report_storage.ReportStore, "write_bytes", publish)
    monkeypatch.setattr(report_storage.ReportStore, "read_bytes", no_alias_read)
    returned = write_report_tree(_decision_state(), "AAPL", tmp_path)
    assert returned != alias and returned.is_file()
    assert alias.read_bytes() == edited
    assert edited not in returned.read_bytes()


def test_windows_creator_requests_create_new_and_protects_open_file(tmp_path, monkeypatch):
    from tradingagents import policy_evidence_acquisition as acquisition

    descriptor = os.open(tmp_path / "model.md", os.O_RDWR | os.O_CREAT | os.O_EXCL, 0o600)
    calls = []

    class Api:
        def CreateFileW(self, *args):
            calls.append(args)
            return 123

        def CloseHandle(self, handle):
            pytest.fail("descriptor owns the successful handle")

    monkeypatch.setattr(acquisition, "_file_api", lambda: (Api(), None))
    monkeypatch.setitem(
        sys.modules, "msvcrt", SimpleNamespace(open_osfhandle=lambda handle, flags: descriptor)
    )
    with report_storage._open_windows_exclusive(tmp_path / "requested.md") as stream:
        stream.write(b"MODEL")
        stream.flush()
    assert calls[0][1:6] == (0xC0000000, 1, None, 1, 0x00200080)


@pytest.mark.parametrize("reason", list(AnalysisOutcomeReason))
def test_chinese_outcome_has_translation_for_each_typed_reason(reason):
    assert reason.value in _OUTCOME_ZH


def test_chinese_blocked_export_keeps_canonical_outcome_and_audit(tmp_path):
    state = _outcome_state()
    state["decision_audit_created_at"] = "2026-07-20T12:30:00+00:00"
    expected = copy.deepcopy(state["analysis_outcome_contract"])
    english = copy.deepcopy(state)
    complete = write_report_tree(state, "AAPL", tmp_path / "zh", output_language="Chinese")
    write_report_tree(english, "AAPL", tmp_path / "en")
    body = (export_root(tmp_path / "zh") / "5_portfolio/analysis_outcome.md").read_text(
        encoding="utf-8"
    )
    assert "非方向性分析结果" in body and "**原因代码:**" in body
    assert "确定性阻断原因" in body and "权威标的身份不可用或无效。" in body
    assert "未形成交易决定。" in body and "模型工作开始前停止" in body
    assert "Non-Directional" not in body and "Deterministic Blockers" not in body
    assert "preflight_blocked" in body
    assert "非方向性分析结果" in complete.read_text(encoding="utf-8")
    assert state["analysis_outcome_contract"] == expected
    assert (tmp_path / "zh/decision-audit.json").read_bytes() == (
        tmp_path / "en/decision-audit.json"
    ).read_bytes()


@pytest.mark.parametrize("code", list(AnalysisDiagnosticCode))
def test_chinese_deterministic_blocker_descriptions_are_complete(code):
    assert code.value in _BLOCKER_ZH


@pytest.mark.parametrize("opened_reparse", (False, True))
def test_windows_handle_adapter_uses_no_follow_and_denies_write_delete_sharing(
    tmp_path, monkeypatch, opened_reparse
):
    # Exercise the real Python adapter with an in-memory API. No WinDLL, real
    # Windows handle, junction or native policy object is created.
    from tradingagents import policy_evidence_acquisition as acquisition

    before = tmp_path.lstat()

    class Info(ctypes.Structure):
        _fields_ = [
            (name, ctypes.c_uint32) for name in ("attributes", "index_high", "index_low", "links")
        ]

    calls = []

    class Api:
        def CreateFileW(self, *args):
            calls.append(args)
            return 123

        def GetFileInformationByHandle(self, handle, pointer):
            info = pointer._obj
            info.attributes = 16 | (0x400 if opened_reparse else 0)
            info.index_high = before.st_ino >> 32
            info.index_low = before.st_ino & 0xFFFFFFFF
            info.links = 1
            return True

        def CloseHandle(self, handle):
            calls.append(("closed", handle))

    monkeypatch.setattr(acquisition, "_file_api", lambda: (Api(), Info))
    monkeypatch.setattr(acquisition, "os", SimpleNamespace(name="nt"))
    if opened_reparse:
        with (
            pytest.raises(ValueError, match="reparse/alias/type"),
            acquisition._held(tmp_path, True, stable_metadata=False),
        ):
            pytest.fail("must reject opened reparse object")
    else:
        with acquisition._held(tmp_path, True, stable_metadata=False):
            pass
    assert calls[0][1:6] == (0x80000000, 1, None, 3, 0x02200000)
    assert calls[-1] == ("closed", 123)

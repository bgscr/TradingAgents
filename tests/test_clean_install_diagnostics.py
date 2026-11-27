"""Synthetic failure classification only. These fixtures do not issue a release."""
import hashlib
import json
import sys

import pytest
from tests.test_bounded_mainland_entry import entry
from scripts import full_calendar_cache as calendar


def test_missing_install_at_entry(tmp_path, monkeypatch, capsys):
    from types import SimpleNamespace
    module = entry()
    monkeypatch.setattr(module, "SOURCE", tmp_path)
    monkeypatch.setattr(module, "sys", SimpleNamespace(flags=SimpleNamespace(isolated=True), dont_write_bytecode=True,
                                                       stderr=sys.stderr))
    assert module.cli_exit() == 1
    receipt = json.loads(capsys.readouterr().out)
    assert (receipt["reason"], receipt["diagnostic"]) == ("missing_bound_file", "reviewed_install_manifest_missing")
    assert not list(tmp_path.iterdir())


@pytest.mark.parametrize("group, diagnostic", [("python", "bound_runtime_mismatch"), ("project", "bound_project_mismatch")])
def test_changed_bound_file_preserves_rejection(tmp_path, monkeypatch, capsys, group, diagnostic):
    module = entry()
    fixture = tmp_path / "ordinary.txt"
    fixture.write_bytes(b"changed")
    manifest = {"contract": "bounded-mainland-release-v1", "deployment_root": str(tmp_path),
                "files": [{"root": group, "relative": "ordinary.txt", "sha256": hashlib.sha256(b"expected").hexdigest()}]}
    monkeypatch.setattr(module, "main", lambda *_: module.verify_release(manifest, source=tmp_path, python_root=tmp_path))
    assert module.cli_exit() == 1
    receipt = json.loads(capsys.readouterr().out)
    assert (receipt["reason"], receipt["diagnostic"]) == ("bound_source_changed", diagnostic)


def test_market_reader_distinguishes_missing_store_from_missing_owner_marker(tmp_path):
    module = entry()
    functions = vars(module)
    store, marker = tmp_path / "market.sqlite3", tmp_path / "owner"
    with pytest.raises(ValueError, match="^local_market_store_unavailable$"):
        calendar.verify_market_store_paths(store, marker, functions)
    store.write_bytes(b"synthetic")
    with pytest.raises(module.Stop, match="^missing_bound_file$"):
        calendar.verify_market_store_paths(store, marker, functions)


@pytest.mark.parametrize("reason", ["local_market_store_unavailable", "prepared SSE calendar range incomplete",
                                    "prepared SZSE calendar range incomplete", "calendar_store_owner_mismatch"])
def test_check_only_receipt_preserves_market_reason(monkeypatch, capsys, reason):
    module = entry()
    def stopped(*args):
        raise module.Stop(reason)
    monkeypatch.setattr(module, "main", stopped)
    assert module.cli_exit() == 1
    receipt = json.loads(capsys.readouterr().out)
    assert receipt["reason"] == reason
    if reason == "calendar_store_owner_mismatch":
        assert "diagnostic" not in receipt
    else:
        assert receipt["diagnostic"] == "local_market_evidence_missing"

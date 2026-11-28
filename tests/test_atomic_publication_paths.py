import errno
import io
import os
from pathlib import Path

import pytest

from tests.test_decision_audit import _artifact_backed_outcome_state
from tradingagents.decision_audit import write_immutable_decision_audit
from tradingagents.market_history import (
    DataUsageMode,
    MarketHistoryConfig,
    MarketHistoryMode,
    MarketHistoryStore,
)

OWNER = ".market-history-database-owner"
AUDIT = "decision-audit-2026-07-18-0000000000000000.json"


def _claim_owner(directory, database="main.sqlite3"):
    MarketHistoryStore._claim_payload_root(MarketHistoryConfig(
        mode=MarketHistoryMode.SHADOW,
        database_path=directory.parent / database,
        payload_root=directory,
        backup_root=directory.parent / "backups",
        data_usage_mode=DataUsageMode.PERSONAL_RESEARCH,
    ))
    return directory / OWNER


def _write_audit(directory):
    state, _ = _artifact_backed_outcome_state("synthetic audit")
    state["evidence_state"] = {}
    return write_immutable_decision_audit(state, directory, filename=AUDIT)


@pytest.mark.unit
@pytest.mark.parametrize(("publish", "filename", "expected_length"), [
    pytest.param(_claim_owner, OWNER, 244, id="cache-owner"),
    pytest.param(_write_audit, AUDIT, 255, id="decision-audit"),
])
@pytest.mark.parametrize("final_fits", [True, False], ids=["fits", "too-long"])
def test_publication_path_budget(tmp_path, monkeypatch, publish, filename, expected_length, final_fits):
    target = tmp_path / filename
    last_rejected = None
    cap = expected_length - int(not final_fits)
    # Model the reported parent length without requiring filesystem support for long paths.
    parent_length = expected_length - len(filename)
    real_io_open, real_os_open, real_link = io.open, os.open, os.link

    def check(path):
        nonlocal last_rejected
        path = Path(path)
        if path.parent == tmp_path and parent_length + len(path.name) > cap:
            last_rejected = path
            raise FileNotFoundError(errno.ENOENT, "synthetic path limit")

    def exclusive_io(path, mode="r", *args, **kwargs):
        if mode == "xb":
            check(path)
        return real_io_open(path, mode, *args, **kwargs)

    def exclusive_os(path, flags, *args, **kwargs):
        if flags & os.O_CREAT:
            check(path)
        return real_os_open(path, flags, *args, **kwargs)

    def link(source, destination, *args, **kwargs):
        assert Path(source).parent == Path(destination).parent == tmp_path
        check(source)
        check(destination)
        return real_link(source, destination, *args, **kwargs)

    monkeypatch.setattr(io, "open", exclusive_io)
    monkeypatch.setattr(os, "open", exclusive_os)
    monkeypatch.setattr(os, "link", link)
    if final_fits:
        assert publish(tmp_path) == target
        assert list(tmp_path.iterdir()) == [target]
    else:
        with pytest.raises(FileNotFoundError) as exc_info:
            publish(tmp_path)
        assert exc_info.value.errno == errno.ENOENT and last_rejected == target
        assert not list(tmp_path.iterdir())


@pytest.mark.unit
def test_owner_temp_collision_preserves_unowned_file(tmp_path, monkeypatch):
    real_open = io.open
    collision = None

    def reserve_first_candidate(path, mode="r", *args, **kwargs):
        nonlocal collision
        if mode == "xb" and Path(path).parent == tmp_path and collision is None:
            collision = Path(path)
            with real_open(path, "xb") as stream:
                stream.write(b"reserved by another writer")
        return real_open(path, mode, *args, **kwargs)

    monkeypatch.setattr(io, "open", reserve_first_candidate)
    with pytest.raises(FileExistsError):
        _claim_owner(tmp_path)
    assert list(tmp_path.iterdir()) == [collision]
    assert collision.read_bytes() == b"reserved by another writer"


@pytest.mark.unit
def test_audit_temp_collision_retries_without_touching_unowned_file(tmp_path, monkeypatch):
    real_open = os.open
    collision = None

    def reserve_first_candidate(path, flags, *args, **kwargs):
        nonlocal collision
        if flags & os.O_EXCL and Path(path).parent == tmp_path and collision is None:
            collision = Path(path)
            collision.write_bytes(b"reserved by another writer")
        return real_open(path, flags, *args, **kwargs)

    monkeypatch.setattr(os, "open", reserve_first_candidate)
    audit = _write_audit(tmp_path)
    assert set(tmp_path.iterdir()) == {audit, collision}
    assert collision.read_bytes() == b"reserved by another writer"


@pytest.mark.unit
@pytest.mark.parametrize("at_creation", [True, False], ids=["temp-creation", "publication"])
def test_owner_permission_errors_cleanup(tmp_path, monkeypatch, at_creation):
    denial = PermissionError(errno.EACCES, "synthetic permission denial")
    real_open = io.open

    def deny_temp(path, mode="r", *args, **kwargs):
        if mode == "xb" and Path(path).parent == tmp_path:
            raise denial
        return real_open(path, mode, *args, **kwargs)

    if at_creation:
        monkeypatch.setattr(io, "open", deny_temp)
    else:
        def deny_link(*_args, **_kwargs):
            raise denial

        monkeypatch.setattr(os, "link", deny_link)
    with pytest.raises(PermissionError) as exc_info:
        _claim_owner(tmp_path)
    assert exc_info.value is denial and not list(tmp_path.iterdir())


@pytest.mark.unit
@pytest.mark.parametrize("deny_creation", ["temp", "final", None])
def test_audit_permission_errors_and_exclusive_fallback(tmp_path, monkeypatch, deny_creation):
    denial = PermissionError(errno.EACCES, "synthetic permission denial")
    real_open, real_access = os.open, os.access

    def deny_open(path, flags, *args, **kwargs):
        if flags & os.O_CREAT and Path(path).parent == tmp_path:
            assert flags & os.O_EXCL
            kind = "final" if Path(path).name == AUDIT else "temp"
            if kind == deny_creation:
                raise denial
        return real_open(path, flags, *args, **kwargs)

    def deny_access(path, mode, *args, **kwargs):
        if deny_creation == "temp" and Path(path) == tmp_path and mode == os.W_OK:
            return False
        return real_access(path, mode, *args, **kwargs)

    def deny_link(*_args, **_kwargs):
        raise denial

    monkeypatch.setattr(os, "open", deny_open)
    monkeypatch.setattr(os, "access", deny_access)
    monkeypatch.setattr(os, "link", deny_link)
    if deny_creation is None:
        audit = _write_audit(tmp_path)
        assert audit.is_file() and list(tmp_path.iterdir()) == [audit]
    else:
        with pytest.raises(PermissionError) as exc_info:
            _write_audit(tmp_path)
        assert exc_info.value is denial and not list(tmp_path.iterdir())


@pytest.mark.unit
def test_existing_owner_and_audit_are_not_overwritten(tmp_path):
    owner = _claim_owner(tmp_path)
    expected_owner = owner.read_bytes()
    assert _claim_owner(tmp_path).read_bytes() == expected_owner
    with pytest.raises(ValueError, match="already owned"):
        _claim_owner(tmp_path, database="another.sqlite3")
    audit = tmp_path / AUDIT
    audit.write_bytes(b"existing audit")
    with pytest.raises(FileExistsError):
        _write_audit(tmp_path)
    assert owner.read_bytes() == expected_owner
    assert audit.read_bytes() == b"existing audit"
    assert set(tmp_path.iterdir()) == {audit, owner}


@pytest.mark.unit
@pytest.mark.parametrize("case", ["absolute", "parent", "nested", "backslash", "drive", "stream", "generated"])
def test_audit_rejects_nonlocal_target_before_preparing_evidence(tmp_path, case):
    directory = tmp_path / "audits"
    nested = directory / "nested"
    nested.mkdir(parents=True)
    sentinel = tmp_path / "existing.json"
    sentinel.write_bytes(b"unrelated target")
    filenames = {
        "absolute": str(tmp_path / "new.json"),
        "parent": "../existing.json",
        "nested": "nested/new.json",
        "backslash": r"nested\new.json",
        "drive": "C:new.json",
        "stream": "decision-audit.json:extra",
        "generated": None,
    }
    state, _ = _artifact_backed_outcome_state("synthetic audit")
    if case == "generated":
        state["trade_date"] = "../new"
    with pytest.raises(ValueError, match="filename must be one path component"):
        write_immutable_decision_audit(state, directory, filename=filenames[case])
    assert sentinel.read_bytes() == b"unrelated target"
    assert set(tmp_path.iterdir()) == {sentinel, directory}
    assert list(directory.iterdir()) == [nested] and not list(nested.iterdir())

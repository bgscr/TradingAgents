"""Exercise the probe classifier with the installed pytest temp factory."""

import errno
import json
import os
from pathlib import Path

import pytest
from _pytest import tmpdir

from tests import issue9_stock_basic_probe as probe

PRIVATE_ERROR = "fixture-private-error-must-not-be-reported"


@pytest.mark.parametrize(
    ("fault", "operation"),
    [
        ("resolve", "pytest_temp_root_resolution"),
        ("user_mkdir", "pytest_temp_user_directory_create"),
        ("numbered_scan", "pytest_temp_numbered_directory_scan"),
        ("numbered_mkdir", "pytest_temp_numbered_directory_create"),
        ("unclassified", "probe_setup"),
    ],
)
def test_temp_setup_operation_from_installed_pytest(tmp_path, monkeypatch, fault, operation):
    # Direct factory call under a test-owned parent. No addopts, application or provider code.
    monkeypatch.setenv("PYTEST_DEBUG_TEMPROOT", str(tmp_path))
    factory = tmpdir.TempPathFactory(None, 0, "all", lambda *_: None, _ispytest=True)
    user_dirs = {tmp_path / "pytest-of-fixture", tmp_path / "pytest-of-unknown"}
    resolve, mkdir, scandir = Path.resolve, Path.mkdir, os.scandir

    def deny(path):
        raise PermissionError(errno.EACCES, PRIVATE_ERROR, os.fspath(path), 5)

    def resolve_path(path, *args, **kwargs):
        if fault == "resolve" and path == tmp_path:
            deny(path)
        return resolve(path, *args, **kwargs)

    def make_directory(path, *args, **kwargs):
        if fault == "user_mkdir" and path in user_dirs:
            deny(path)
        if fault == "numbered_mkdir" and path.parent in user_dirs:
            deny(path)
        return mkdir(path, *args, **kwargs)

    def scan_directory(path):
        if fault == "numbered_scan" and path in user_dirs:
            deny(path)
        return scandir(path)

    monkeypatch.setattr(tmpdir, "get_user", lambda: "fixture")
    monkeypatch.setattr(Path, "resolve", resolve_path)
    monkeypatch.setattr(Path, "mkdir", make_directory)
    monkeypatch.setattr(os, "scandir", scan_directory)
    if fault == "unclassified":
        monkeypatch.setattr(tmpdir, "get_user", lambda: deny(tmp_path))

    with pytest.raises(OSError) as caught:
        factory.getbasetemp()
    expected = {"operation": operation, "exception": "OSError", "errno": None, "winerror": None, "path_chars": None}
    if fault != "numbered_mkdir":
        expected.update(
            exception="PermissionError",
            errno=errno.EACCES,
            winerror=5 if os.name == "nt" else None,
            path_chars=len(os.fspath(caught.value.filename)),
        )
    observed = probe.pytest_setup_diagnostic(caught.value)
    assert observed == expected and PRIVATE_ERROR not in json.dumps(observed)
    if fault == "resolve":
        with monkeypatch.context() as no_source:
            no_source.setattr(probe.linecache, "getline", lambda *_: "")
            fallback = probe.pytest_setup_diagnostic(caught.value)
        assert fallback == expected | {"operation": "probe_setup"}

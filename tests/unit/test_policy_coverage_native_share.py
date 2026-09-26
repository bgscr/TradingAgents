"""Actual share-mode feasibility on fresh disposable directories only."""

import ctypes
import os

import pytest

from tests.fixtures.policy_coverage_fixture import junction_buffer


def assign_junction_to_existing_empty_directory(path, target):
    from ctypes import wintypes

    from tradingagents.policy_evidence_acquisition import _file_api

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
    handle = api.CreateFileW(str(path), 0x40000000, 3, None, 3, 0x02200000, None)
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


def test_directory_data_read_handle_denies_rename_and_reparse_assignment(tmp_path):
    from tradingagents.policy_evidence_acquisition import _file_api

    path = tmp_path / "subject"
    path.mkdir()
    target = tmp_path / "outside-scope"
    target.mkdir()
    api, _ = _file_api()
    handle = api.CreateFileW(str(path), 0x80000000, 1, None, 3, 0x02300000, None)
    if handle == ctypes.c_void_p(-1).value:
        raise ctypes.WinError(ctypes.get_last_error())
    try:
        with pytest.raises(OSError):
            os.rename(path, tmp_path / "renamed-subject")
        with pytest.raises(OSError):
            assign_junction_to_existing_empty_directory(path, target)
    finally:
        api.CloseHandle(handle)
    assert not path.lstat().st_file_attributes & 0x400


@pytest.mark.parametrize("module_name", ["policy_coverage_native", "policy_evidence_acquisition"])
def test_real_measurement_and_publication_holders_deny_reparse_conversion(tmp_path, module_name):
    import importlib

    module = importlib.import_module("tradingagents." + module_name)
    subject = tmp_path / "subject"
    subject.mkdir()
    target = tmp_path / "other-disposable-scope"
    target.mkdir()
    with module._held(subject, True), pytest.raises(OSError):
        assign_junction_to_existing_empty_directory(subject, target)
    assert not subject.lstat().st_file_attributes & 0x400

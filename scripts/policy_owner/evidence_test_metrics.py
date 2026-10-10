"""Resource observations for owned evidence fixtures, not host/history forensics."""

import ctypes
import os
from pathlib import Path


def memory_metrics():
    from ctypes import wintypes

    class Counters(ctypes.Structure):
        _fields_ = [("cb", wintypes.DWORD), ("faults", wintypes.DWORD)] + [
            (name, ctypes.c_size_t)
            for name in (
                "peak_working_set",
                "working_set",
                "quota_peak_paged",
                "quota_paged",
                "quota_peak_nonpaged",
                "quota_nonpaged",
                "pagefile",
                "peak_pagefile",
                "private",
            )
        ]

    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel.GetCurrentProcess.restype = wintypes.HANDLE
    api = ctypes.WinDLL("psapi", use_last_error=True).GetProcessMemoryInfo
    api.argtypes = [wintypes.HANDLE, ctypes.c_void_p, wintypes.DWORD]
    value = Counters()
    value.cb = ctypes.sizeof(value)
    if not api(kernel.GetCurrentProcess(), ctypes.byref(value), value.cb):
        return {
            "process_memory_observation_available": False,
            "native_error": ctypes.get_last_error(),
        }
    return {
        "process_memory_observation_available": True,
        "process_peak_working_set_bytes": value.peak_working_set,
        "process_peak_commit_bytes": value.peak_pagefile,
        "process_current_private_bytes": value.private,
        "scope": "this fixture process; excludes system filesystem cache and separate child peaks",
    }


def disk_metrics(root: Path):
    from ctypes import wintypes

    class Standard(ctypes.Structure):
        _fields_ = [
            ("allocation", ctypes.c_longlong),
            ("end", ctypes.c_longlong),
            ("links", wintypes.DWORD),
            ("delete_pending", ctypes.c_byte),
            ("directory", ctypes.c_byte),
        ]

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
    api.GetFileInformationByHandleEx.argtypes = [
        wintypes.HANDLE,
        ctypes.c_int,
        ctypes.c_void_p,
        wintypes.DWORD,
    ]
    api.CloseHandle.argtypes = [wintypes.HANDLE]
    logical = allocated = files = skipped = 0
    unique = set()

    def walk(directory):
        nonlocal logical, allocated, files, skipped
        with os.scandir(directory) as entries:
            for entry in entries:
                path = Path(entry.path)
                info = path.lstat()
                if info.st_file_attributes & 0x400:
                    skipped += 1
                    continue
                if entry.is_dir(follow_symlinks=False):
                    walk(path)
                    continue
                files += 1
                logical += info.st_size
                key = info.st_dev, info.st_ino
                if key in unique:
                    continue
                unique.add(key)
                handle = api.CreateFileW(str(path), 0x80, 7, None, 3, 0x02200000, None)
                if handle == ctypes.c_void_p(-1).value:
                    raise ctypes.WinError(ctypes.get_last_error())
                try:
                    value = Standard()
                    if not api.GetFileInformationByHandleEx(
                        handle, 1, ctypes.byref(value), ctypes.sizeof(value)
                    ):
                        raise ctypes.WinError(ctypes.get_last_error())
                    allocated += value.allocation
                finally:
                    api.CloseHandle(handle)

    walk(root)
    return {
        "logical_file_bytes": logical,
        "allocated_file_bytes": allocated,
        "file_entries": files,
        "unique_file_objects": len(unique),
        "reparse_entries_not_followed": skipped,
        "allocation_scope": "Windows FileStandardInfo allocation, deduplicated hardlinks; excludes directory/volume metadata",
    }

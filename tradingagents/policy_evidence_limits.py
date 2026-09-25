"""Finite bounds on an owned evidence CLI process and its descendants only."""

from __future__ import annotations

import ctypes
import os
import threading

_retained_handles = []
_retained_timers = []


def bound_evidence_process(*, seconds: int, memory_bytes: int = 4 * 1024**3) -> None:
    """Keep the job alive until process exit; never attach to another process."""
    if (
        os.name != "nt"
        or not 1 <= seconds <= 3660
        or not 64 * 1024**2 <= memory_bytes <= 4 * 1024**3
    ):
        raise ValueError("unsupported finite evidence process bounds")
    from ctypes import wintypes

    class Basic(ctypes.Structure):
        _fields_ = [
            ("process_time", ctypes.c_longlong),
            ("job_time", ctypes.c_longlong),
            ("flags", wintypes.DWORD),
            ("minimum", ctypes.c_size_t),
            ("maximum", ctypes.c_size_t),
            ("active", wintypes.DWORD),
            ("affinity", ctypes.c_size_t),
            ("priority", wintypes.DWORD),
            ("scheduling", wintypes.DWORD),
        ]

    class Counters(ctypes.Structure):
        _fields_ = [
            (name, ctypes.c_ulonglong)
            for name in (
                "read_ops",
                "write_ops",
                "other_ops",
                "read_bytes",
                "write_bytes",
                "other_bytes",
            )
        ]

    class Extended(ctypes.Structure):
        _fields_ = [
            ("basic", Basic),
            ("io", Counters),
            ("process_memory", ctypes.c_size_t),
            ("job_memory", ctypes.c_size_t),
            ("peak_process", ctypes.c_size_t),
            ("peak_job", ctypes.c_size_t),
        ]

    api = ctypes.WinDLL("kernel32", use_last_error=True)
    api.CreateJobObjectW.argtypes = [ctypes.c_void_p, wintypes.LPCWSTR]
    api.CreateJobObjectW.restype = wintypes.HANDLE
    api.SetInformationJobObject.argtypes = [
        wintypes.HANDLE,
        ctypes.c_int,
        ctypes.c_void_p,
        wintypes.DWORD,
    ]
    api.AssignProcessToJobObject.argtypes = [wintypes.HANDLE, wintypes.HANDLE]
    api.GetCurrentProcess.restype = wintypes.HANDLE
    api.TerminateJobObject.argtypes = [wintypes.HANDLE, wintypes.UINT]
    api.CloseHandle.argtypes = [wintypes.HANDLE]
    job = api.CreateJobObjectW(None, None)
    if not job:
        raise ctypes.WinError(ctypes.get_last_error())
    limits = Extended()
    limits.basic.flags = 0x2300  # kill on close, process and total job committed memory
    limits.process_memory = limits.job_memory = memory_bytes
    if not api.SetInformationJobObject(
        job, 9, ctypes.byref(limits), ctypes.sizeof(limits)
    ) or not api.AssignProcessToJobObject(job, api.GetCurrentProcess()):
        error = ctypes.get_last_error()
        api.CloseHandle(job)
        raise ctypes.WinError(error)
    # Closing this handle intentionally kills this process. Retain it, letting
    # Windows close it at exit; no long-lived scheduler or machine setting exists.
    _retained_handles.append(job)
    timer = threading.Timer(seconds, lambda: api.TerminateJobObject(job, 124))
    timer.daemon = True
    _retained_timers.append(timer)
    timer.start()

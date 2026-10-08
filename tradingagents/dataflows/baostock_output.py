"""Thread-aware containment for output emitted directly by the BaoStock SDK."""

from __future__ import annotations

import sys
import threading
from collections.abc import Iterable, Iterator
from contextlib import contextmanager
from typing import TextIO


_BAOSTOCK_OUTPUT_LOCK = threading.RLock()
_SUPPRESSED_THREAD_COUNTS: dict[int, int] = {}


def _current_thread_is_suppressed() -> bool:
    with _BAOSTOCK_OUTPUT_LOCK:
        return _SUPPRESSED_THREAD_COUNTS.get(threading.get_ident(), 0) > 0


class _ThreadFilteredStream:
    """Share one filter across SDK threads without serializing their calls."""

    def __init__(self, stream: TextIO) -> None:
        self._stream = stream

    def write(self, value: str) -> int:
        if _current_thread_is_suppressed():
            return len(value)
        # Never hold the bookkeeping lock across arbitrary stream I/O.
        return self._stream.write(value)

    def writelines(self, values: Iterable[str]) -> None:
        for value in values:
            self.write(value)

    def flush(self) -> None:
        if not _current_thread_is_suppressed():
            self._stream.flush()

    def __getattr__(self, name: str) -> object:
        return getattr(self._stream, name)


_STDOUT_PROXY: _ThreadFilteredStream | None = None
_STDERR_PROXY: _ThreadFilteredStream | None = None


def _unfiltered_stream(stream: TextIO) -> TextIO:
    # An external redirect may restore a retired proxy after our final exit.
    # Avoid accumulating wrappers when a later containment scope starts.
    while isinstance(stream, _ThreadFilteredStream):
        stream = stream._stream
    return stream


@contextmanager
def contain_baostock_output() -> Iterator[None]:
    """Discard text writes from every thread currently inside this scope.

    Only installation and reference counts are locked. In particular, a
    control thread can log out while another thread is blocked in an SDK query.
    Nested and overlapping scopes may exit in any order.

    As with contextlib redirects, concurrent external replacement of the
    process-wide sys streams is outside the containment guarantee. Cleanup
    never overwrites a replacement installed by somebody else. Retired proxies
    remain transparent after the final scope exits. Binary/descriptor writes
    and SDK-created threads that do not enter this scope are not intercepted.
    """

    global _STDOUT_PROXY, _STDERR_PROXY

    thread_id = threading.get_ident()
    with _BAOSTOCK_OUTPUT_LOCK:
        if not _SUPPRESSED_THREAD_COUNTS:
            _STDOUT_PROXY = _ThreadFilteredStream(_unfiltered_stream(sys.stdout))
            _STDERR_PROXY = _ThreadFilteredStream(_unfiltered_stream(sys.stderr))
            sys.stdout = _STDOUT_PROXY
            sys.stderr = _STDERR_PROXY
        _SUPPRESSED_THREAD_COUNTS[thread_id] = (
            _SUPPRESSED_THREAD_COUNTS.get(thread_id, 0) + 1
        )

    try:
        yield
    finally:
        with _BAOSTOCK_OUTPUT_LOCK:
            remaining = _SUPPRESSED_THREAD_COUNTS[thread_id] - 1
            if remaining:
                _SUPPRESSED_THREAD_COUNTS[thread_id] = remaining
            else:
                del _SUPPRESSED_THREAD_COUNTS[thread_id]
            if not _SUPPRESSED_THREAD_COUNTS:
                stdout_proxy, stderr_proxy = _STDOUT_PROXY, _STDERR_PROXY
                if stdout_proxy is not None and sys.stdout is stdout_proxy:
                    sys.stdout = stdout_proxy._stream
                if stderr_proxy is not None and sys.stderr is stderr_proxy:
                    sys.stderr = stderr_proxy._stream
                _STDOUT_PROXY = None
                _STDERR_PROXY = None


__all__ = ["contain_baostock_output"]

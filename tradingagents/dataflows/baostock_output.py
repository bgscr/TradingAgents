"""Thread-aware containment for output emitted directly by the BaoStock SDK."""

from __future__ import annotations

import sys
import threading
from collections.abc import Iterable, Iterator
from contextlib import contextmanager, redirect_stderr, redirect_stdout
from typing import TextIO


class _ThreadFilteredStream:
    """Discard writes from one SDK thread while forwarding all other writers."""

    def __init__(self, stream: TextIO, *, suppressed_thread_id: int) -> None:
        self._stream = stream
        self._suppressed_thread_id = suppressed_thread_id

    def write(self, value: str) -> int:
        if threading.get_ident() == self._suppressed_thread_id:
            return len(value)
        return self._stream.write(value)

    def writelines(self, values: Iterable[str]) -> None:
        for value in values:
            self.write(value)

    def flush(self) -> None:
        if threading.get_ident() != self._suppressed_thread_id:
            self._stream.flush()

    def __getattr__(self, name: str) -> object:
        return getattr(self._stream, name)


_BAOSTOCK_OUTPUT_LOCK = threading.RLock()


@contextmanager
def contain_baostock_output() -> Iterator[None]:
    """Discard only output written by the thread executing a BaoStock operation."""

    thread_id = threading.get_ident()
    with (
        _BAOSTOCK_OUTPUT_LOCK,
        redirect_stdout(_ThreadFilteredStream(sys.stdout, suppressed_thread_id=thread_id)),
        redirect_stderr(_ThreadFilteredStream(sys.stderr, suppressed_thread_id=thread_id)),
    ):
        yield


__all__ = ["contain_baostock_output"]

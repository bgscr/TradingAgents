from __future__ import annotations

import codecs
import sys
from typing import TextIO


def _uses_utf8(stream: TextIO) -> bool:
    encoding = getattr(stream, "encoding", None)
    if not encoding:
        return True
    try:
        return codecs.lookup(encoding).name == "utf-8"
    except LookupError:
        return False


def _reconfigure_utf8(stream: TextIO) -> None:
    if _uses_utf8(stream):
        return
    reconfigure = getattr(stream, "reconfigure", None)
    if not callable(reconfigure):
        return
    try:
        reconfigure(encoding="utf-8", errors="backslashreplace")
    except (OSError, ValueError):
        # Some redirected or host-provided streams cannot be reconfigured.
        # Rich retains its existing fallback behavior for those streams.
        return


def configure_utf8_stdio(
    *,
    stdout: TextIO | None = None,
    stderr: TextIO | None = None,
) -> None:
    """Make CLI streams accept arbitrary report text without changing artifacts."""

    _reconfigure_utf8(sys.stdout if stdout is None else stdout)
    _reconfigure_utf8(sys.stderr if stderr is None else stderr)

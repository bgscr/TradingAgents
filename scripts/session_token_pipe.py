"""One-run, stdin-only Tushare handoff for the reviewed bounded entry.

No files, environment credential reads, sockets, logging, or CLI token arguments.
This limits accidental disclosure by trusted code; it is not an OS sandbox or
a guarantee of memory erasure against a debugger, an administrator, or a dump.
"""
from __future__ import annotations

import os
import stat
import subprocess

PREFIX = b'tushare-session-token-v1\n'
MAX_TOKEN_BYTES = 512
MAX_FRAME_BYTES = len(PREFIX) + 32 + 1 + MAX_TOKEN_BYTES + 1


class TokenPipeError(RuntimeError):
    """Fixed error codes only, never input or upstream exception text."""


def _run_id(value):
    if (type(value) is not str or len(value) != 32
            or any(char not in '0123456789abcdef' for char in value)):
        raise TokenPipeError('session_token_binding_invalid')
    return value.encode('ascii')


def _token(value):
    if (type(value) is not str or not 0 < len(value) <= MAX_TOKEN_BYTES
            or any(not 32 < ord(char) < 127 for char in value)):
        raise TokenPipeError('session_token_input_invalid')
    return value


def encode_session_token(token, run_id):
    """A small binary frame. The non-secret run ID prevents accidental misrouting."""
    return PREFIX + _run_id(run_id) + b'\n' + _token(token).encode('ascii') + b'\n'


def require_pipe(stream):
    """Reject TTYs and regular-file redirection; do not consume coordinator input."""
    try:
        valid = stat.S_ISFIFO(os.fstat(stream.fileno()).st_mode)
    except (AttributeError, OSError, TypeError, ValueError):
        valid = False
    if not valid:
        raise TokenPipeError('session_token_pipe_required')


def read_session_token(stream, run_id):
    """Read once after worker validation/guard setup, then close even on failure.

    The parent must close the writer. The coordinator's existing worker timeout
    bounds a missing EOF; this function never prompts or falls back to a file.
    """
    expected = PREFIX + _run_id(run_id) + b'\n'
    raw = None
    try:
        require_pipe(stream)
        raw = stream.read(MAX_FRAME_BYTES + 1)
        if (type(raw) is not bytes or len(raw) > MAX_FRAME_BYTES
                or not raw.startswith(expected) or not raw.endswith(b'\n')):
            raise TokenPipeError('session_token_frame_invalid')
        try:
            return _token(raw[len(expected):-1].decode('ascii'))
        except UnicodeError:
            raise TokenPipeError('session_token_input_invalid') from None
    finally:
        raw = None
        stream.close()


def _wait_coordinator(process):
    # Do not kill only the coordinator and orphan its live worker. Its own
    # <=180s worker caps remain responsible for cleanup. The entry already
    # advertises a cooperative deadline, not process-tree hard cancellation.
    while True:
        try:
            return process.wait()
        except KeyboardInterrupt:
            continue


def send_session_token(command, *, token, run_id, cwd, env):
    """Send only to fixed trusted coordinator stdin, not argv or child env.

    If interrupted, close input and wait for the coordinator to reap its child.
    A forced OS termination of the entry/coordinator remains outside this
    cooperative lifecycle; no process-tree kill guarantee is made.
    """
    frame = encode_session_token(token, run_id)
    token = None
    process = None
    try:
        process = subprocess.Popen(command, cwd=cwd, env=env, stdin=subprocess.PIPE,
                                   close_fds=True)
        try:
            process.communicate(input=frame)
        except BaseException:
            while True:
                try:
                    if process.stdin is not None and not process.stdin.closed:
                        process.stdin.close()
                    break
                except KeyboardInterrupt:
                    continue
                except (OSError, ValueError):
                    break
            _wait_coordinator(process)
            raise
        return subprocess.CompletedProcess(command, process.returncode)
    finally:
        frame = None


def run_worker(command, *, cwd, env, timeout, stdin=None):
    """Reap the immediate credential worker on timeout, interruption or error."""
    process = subprocess.Popen(command, cwd=cwd, env=env, stdin=stdin, close_fds=True)
    try:
        returncode = process.wait(timeout=timeout)
        return subprocess.CompletedProcess(command, returncode)
    finally:
        # Include poll/kill in the interruption-resistant region. An OS error
        # from kill remains explicit; it must never look like a successful stop.
        while True:
            try:
                if process.poll() is None:
                    process.kill()
                break
            except KeyboardInterrupt:
                continue
        _wait_coordinator(process)

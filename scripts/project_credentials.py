"""Explicit fixed-project credential input; never search, expand, log or persist keys."""
from __future__ import annotations

import io
import os
import stat
from pathlib import Path

KEYS = ('TUSHARE_TOKEN', 'DEEPSEEK_API_KEY')
MAX_FILE_BYTES = 65536
FAILURE_CODES = frozenset({
    'project_env_linked_path', 'project_env_not_regular', 'project_env_size',
    'project_env_changed', 'project_env_key_scope', 'project_env_syntax',
    'project_env_duplicate_key', 'project_env_invalid_value',
    'project_env_interpolation_forbidden', 'project_env_missing_key', 'project_env_unreadable',
})


class ProjectCredentialError(RuntimeError):
    """A fixed, non-secret input error code."""


def credential_failure_code(error):
    """Never return an exception body, value or filename as a diagnostic."""
    if isinstance(error, ProjectCredentialError) and len(error.args) == 1:
        code = error.args[0]
        if type(code) is str and code in FAILURE_CODES:
            return code
    return None


def _checked_file(source):
    root = Path(source).absolute()
    path = root / '.env'  # No caller-selected file, parent lookup or enterprise fallback.
    for item in (path, *path.parents):
        info = item.lstat()
        if stat.S_ISLNK(info.st_mode) or getattr(info, 'st_file_attributes', 0) & 0x400:
            raise ProjectCredentialError('project_env_linked_path')
    info = path.lstat()
    if not stat.S_ISREG(info.st_mode):
        raise ProjectCredentialError('project_env_not_regular')
    if info.st_nlink != 1:
        raise ProjectCredentialError('project_env_linked_path')
    if not 0 < info.st_size <= MAX_FILE_BYTES:
        raise ProjectCredentialError('project_env_size')
    return path, info


def _identity(info):
    return info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns


def _read_file(source):
    path, before = _checked_file(source)
    flags = os.O_RDONLY | getattr(os, 'O_BINARY', 0) | getattr(os, 'O_NOFOLLOW', 0)
    descriptor = os.open(path, flags)
    try:
        opened = os.fstat(descriptor)
        _, after = _checked_file(source)
        if _identity(opened) != _identity(before) or _identity(after) != _identity(before):
            raise ProjectCredentialError('project_env_changed')
        data = bytearray()
        while len(data) <= MAX_FILE_BYTES:
            part = os.read(descriptor, min(8192, MAX_FILE_BYTES + 1 - len(data)))
            if not part:
                break
            data.extend(part)
        _, final = _checked_file(source)
        if (len(data) != before.st_size or _identity(os.fstat(descriptor)) != _identity(before)
                or _identity(final) != _identity(before)):
            raise ProjectCredentialError('project_env_changed')
        return data.decode('utf-8-sig')
    finally:
        os.close(descriptor)


def read_project_credentials(source, *, names=KEYS):
    """Read only named credentials from SOURCE/.env, with interpolation disabled.

    Values exist only in process memory. All exceptions have fixed codes; raw
    parser input, values, hashes and underlying exception bodies are not retained.
    """
    if names not in (KEYS, ('TUSHARE_TOKEN',)):
        raise ProjectCredentialError('project_env_key_scope')
    try:
        from dotenv.parser import parse_stream

        text = _read_file(source)
        result = {}
        for binding in parse_stream(io.StringIO(text)):
            if binding.error:
                raise ProjectCredentialError('project_env_syntax')
            if binding.key not in names:
                continue
            if binding.key in result:
                raise ProjectCredentialError('project_env_duplicate_key')
            value = binding.value
            if type(value) is not str or not 0 < len(value) <= 512 or any(
                    not 32 < ord(char) < 127 for char in value):
                raise ProjectCredentialError('project_env_invalid_value')
            if '${' in value:
                raise ProjectCredentialError('project_env_interpolation_forbidden')
            if value.startswith(binding.key + '='):
                raise ProjectCredentialError('project_env_invalid_value')
            result[binding.key] = value
        if set(result) != set(names):
            raise ProjectCredentialError('project_env_missing_key')
        return result
    except ProjectCredentialError:
        raise
    except (OSError, UnicodeError, ValueError, ImportError):
        raise ProjectCredentialError('project_env_unreadable') from None

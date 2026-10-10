"""No-follow report publication, independent of policy authority or credentials.

Windows directory handles deny write/delete sharing for the entire publication.
POSIX operations are relative to O_NOFOLLOW directory descriptors. Existing
report files are never opened for truncation; publication replaces directory
entries or creates a new immutable entry.
"""

from __future__ import annotations

import os
import stat
import uuid
from contextlib import ExitStack, suppress
from pathlib import Path


def _ordinary(info, *, directory):
    if stat.S_ISLNK(info.st_mode) or getattr(info, "st_file_attributes", 0) & 0x400:
        raise ValueError("report path is a link or reparse point")
    if directory:
        if not stat.S_ISDIR(info.st_mode):
            raise ValueError("report parent is not a directory")
    elif not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
        raise ValueError("report target is not an ordinary unaliased file")


def _hold_windows_object(path, *, directory):
    # This helper opens with OPEN_REPARSE_POINT, checks the opened object against
    # lstat, and holds FILE_SHARE_READ only. No security/ACL/authority API is used.
    from tradingagents.policy_evidence_acquisition import _held

    return _held(path, directory, stable_metadata=not directory)


class ReportStore:
    """Own only report paths below one root, with pinned directory traversal."""

    def __init__(self, root):
        self.root = Path(os.path.abspath(root))
        self._stack = ExitStack()
        self._directories = {}
        self._windows = os.name == "nt"

    def __enter__(self):
        try:
            if self._windows:
                if self.root.drive.startswith("\\\\"):
                    raise ValueError("report publication requires a local drive")
                for path in (*reversed(self.root.parents), self.root):
                    self._pin_windows_directory(path, create=True)
            else:
                flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
                descriptor = os.open(self.root.anchor, flags)
                self._stack.callback(os.close, descriptor)
                for part in self.root.parts[1:]:
                    with suppress(FileExistsError):
                        os.mkdir(part, dir_fd=descriptor)
                    descriptor = os.open(part, flags, dir_fd=descriptor)
                    self._stack.callback(os.close, descriptor)
                self._directories[()] = descriptor
            return self
        except BaseException:
            self._stack.close()
            raise

    def __exit__(self, *error):
        return self._stack.__exit__(*error)

    def _parts(self, relative):
        relative = Path(relative)
        if relative.is_absolute():
            relative = relative.relative_to(self.root)
        parts = relative.parts
        if any(
            part in {".", ".."}
            or ":" in part
            or "\\" in part
            or part.endswith((".", " "))
            or any(ord(char) < 32 for char in part)
            for part in parts
        ):
            raise ValueError("unsafe report relative path")
        return parts

    def _pin_windows_directory(self, path, *, create):
        if path in self._directories:
            return path
        try:
            info = path.lstat()
        except FileNotFoundError:
            if not create:
                raise
            path.mkdir()
            info = path.lstat()
        _ordinary(info, directory=True)
        self._stack.enter_context(_hold_windows_object(path, directory=True))
        self._directories[path] = path
        return path

    def directory(self, relative=(), *, create=True):
        parts = tuple(relative) if isinstance(relative, tuple) else self._parts(relative)
        if self._windows:
            current = self.root
            for part in parts:
                current = self._pin_windows_directory(current / part, create=create)
            return current
        current = self._directories[()]
        for index, part in enumerate(parts):
            key = parts[: index + 1]
            if key not in self._directories:
                if create:
                    with suppress(FileExistsError):
                        os.mkdir(part, dir_fd=current)
                descriptor = os.open(
                    part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=current
                )
                self._stack.callback(os.close, descriptor)
                self._directories[key] = descriptor
            current = self._directories[key]
        return current

    def inspect(self, relative):
        """Inspect an existing known path without creating or following it."""
        parts = self._parts(relative)
        try:
            parent = self.directory(parts[:-1], create=False)
            info = (
                (parent / parts[-1]).lstat()
                if self._windows
                else os.stat(parts[-1], dir_fd=parent, follow_symlinks=False)
            )
        except FileNotFoundError:
            return None
        _ordinary(info, directory=stat.S_ISDIR(info.st_mode))
        return info

    def read_bytes(self, relative):
        parts = self._parts(relative)
        try:
            parent = self.directory(parts[:-1], create=False)
            if self._windows:
                path = parent / parts[-1]
                _ordinary(path.lstat(), directory=False)
                with _hold_windows_object(path, directory=False):
                    return path.read_bytes()
            descriptor = os.open(parts[-1], os.O_RDONLY | os.O_NOFOLLOW, dir_fd=parent)
            with os.fdopen(descriptor, "rb") as stream:
                _ordinary(os.fstat(stream.fileno()), directory=False)
                return stream.read()
        except FileNotFoundError:
            return None

    def write_bytes(self, relative, data, *, replace=False):
        """Publish flushed bytes without following an existing target."""
        parts = self._parts(relative)
        if not parts:
            raise ValueError("report file path required")
        parent = self.directory(parts[:-1])
        existing = self.inspect(relative)
        if existing is not None:
            _ordinary(existing, directory=False)
            if not replace:
                if self.read_bytes(relative) != data:
                    raise FileExistsError(f"immutable report conflicts: {relative}")
                return
        temporary = ".report-" + uuid.uuid4().hex + ".tmp"
        flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_BINARY", 0)
        if self._windows:
            descriptor = os.open(parent / temporary, flags, 0o600)
        else:
            descriptor = os.open(temporary, flags | os.O_NOFOLLOW, 0o600, dir_fd=parent)
        try:
            with os.fdopen(descriptor, "wb") as stream:
                stream.write(data)
                stream.flush()
                os.fsync(stream.fileno())
            if self._windows:
                if replace:
                    os.replace(parent / temporary, parent / parts[-1])
                else:
                    os.link(parent / temporary, parent / parts[-1], follow_symlinks=False)
            elif replace:
                os.replace(temporary, parts[-1], src_dir_fd=parent, dst_dir_fd=parent)
            else:
                os.link(
                    temporary,
                    parts[-1],
                    src_dir_fd=parent,
                    dst_dir_fd=parent,
                    follow_symlinks=False,
                )
        except FileExistsError:
            if replace or self.read_bytes(relative) != data:
                raise
        finally:
            if self._windows:
                (parent / temporary).unlink(missing_ok=True)
            else:
                with suppress(FileNotFoundError):
                    os.unlink(temporary, dir_fd=parent)

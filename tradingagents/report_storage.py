"""No-follow report publication, independent of policy authority or credentials.

Windows directory handles deny write/delete sharing for the entire publication.
POSIX operations are relative to O_NOFOLLOW directory descriptors. New files
are written and verified through the original open descriptor. No closed
temporary pathname is linked or renamed, and no existing file is replaced.
"""

from __future__ import annotations

import os
import stat
from contextlib import ExitStack, suppress
from hashlib import sha256
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


def _open_windows_exclusive(path):
    """CREATE_NEW with write/delete sharing denied until the stream closes."""
    import ctypes
    import msvcrt

    from tradingagents.policy_evidence_acquisition import _file_api

    api, _ = _file_api()
    handle = api.CreateFileW(str(path), 0xC0000000, 1, None, 1, 0x00200080, None)
    if handle == ctypes.c_void_p(-1).value:
        error = ctypes.get_last_error()
        if error in (80, 183):
            raise FileExistsError(f"report file already exists: {path}")
        raise ctypes.WinError(error)
    try:
        descriptor = msvcrt.open_osfhandle(handle, os.O_RDWR | getattr(os, "O_BINARY", 0))
    except BaseException:
        api.CloseHandle(handle)
        raise
    try:
        return os.fdopen(descriptor, "w+b", buffering=0)
    except BaseException:
        os.close(descriptor)
        raise


def _create_windows_file(path):
    return _open_windows_exclusive(path)


class ReportStore:
    """Own only report paths below one root, with pinned directory traversal."""

    def __init__(self, root):
        self.root = Path(os.path.abspath(root))
        self._stack = ExitStack()
        self._directories = {}
        self._created = []
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
        try:
            if error[0] is None:
                for record in self._created:
                    self._verify_created(*record)
        finally:
            self._stack.__exit__(*error)

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

    def new_directory(self, relative):
        """Allocate this export once; an existing name is a conflict."""
        parts = self._parts(relative)
        parent = self.directory(parts[:-1])
        if self._windows:
            (parent / parts[-1]).mkdir()
        else:
            os.mkdir(parts[-1], dir_fd=parent)
        self.directory(parts)

    def read_bytes(self, relative):
        """Read immutable input while retaining its handles through store exit."""
        parts = self._parts(relative)
        for record in self._created:
            if self._parts(record[0]) == parts:
                self._verify_created(*record)
                record[1].seek(0)
                return record[1].read()
        try:
            parent = self.directory(parts[:-1], create=False)
            if self._windows:
                path = parent / parts[-1]
                _ordinary(path.lstat(), directory=False)
                self._stack.enter_context(_hold_windows_object(path, directory=False))
                stream = path.open("rb", buffering=0)
            else:
                descriptor = os.open(parts[-1], os.O_RDONLY | os.O_NOFOLLOW, dir_fd=parent)
                stream = os.fdopen(descriptor, "rb", buffering=0)
        except FileNotFoundError:
            return None
        self._stack.enter_context(stream)
        info = os.fstat(stream.fileno())
        _ordinary(info, directory=False)
        data = stream.read()
        record = (relative, stream, (info.st_dev, info.st_ino), sha256(data).hexdigest(), len(data))
        self._verify_created(*record)
        self._created.append(record)
        return data

    def _verify_created(self, relative, stream, identity, digest, length):
        info = os.fstat(stream.fileno())
        _ordinary(info, directory=False)
        named = self.inspect(relative)
        if (
            named is None
            or (named.st_dev, named.st_ino) != identity
            or (info.st_dev, info.st_ino) != identity
        ):
            raise ValueError("report object changed while its descriptor was held")
        stream.seek(0)
        observed = sha256()
        count = 0
        while chunk := stream.read(1024 * 1024):
            observed.update(chunk)
            count += len(chunk)
        if count != length or observed.hexdigest() != digest:
            raise ValueError("report content changed while its descriptor was held")

    def write_bytes(self, relative, data):
        """Create, flush and verify the final file through one held descriptor."""
        parts = self._parts(relative)
        if not parts:
            raise ValueError("report file path required")
        parent = self.directory(parts[:-1])
        try:
            if self._windows:
                stream = _create_windows_file(parent / parts[-1])
            else:
                descriptor = os.open(
                    parts[-1],
                    os.O_RDWR | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
                    0o600,
                    dir_fd=parent,
                )
                stream = os.fdopen(descriptor, "w+b", buffering=0)
        except FileExistsError:
            self.inspect(relative)
            raise FileExistsError(f"new report file already exists: {relative}") from None
        self._stack.enter_context(stream)
        info = os.fstat(stream.fileno())
        _ordinary(info, directory=False)
        identity = (info.st_dev, info.st_ino)
        stream.write(data)
        stream.flush()
        os.fsync(stream.fileno())
        record = (relative, stream, identity, sha256(data).hexdigest(), len(data))
        self._verify_created(*record)
        self._created.append(record)

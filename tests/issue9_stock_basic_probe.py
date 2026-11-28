r"""Run one synthetic stock_basic diagnostic from the Windows repository root.

    .\.venv\Scripts\python.exe -I -S -B tests/issue9_stock_basic_probe.py

The usual pytest temp parent and numbered directory are used. A new session
may get a longer counter suffix. Plugin autoload, cache and old-temp pruning
are disabled. Capture uses memory instead of temporary files. Output is one
JSON line. The exit status is pytest's, or 2 if the probe's guard stops it.
Unrecognized pytest setup failures keep the operation "probe_setup".
"""

import contextlib
import ctypes
import io
import json
import linecache
import os
import platform
import re
import sqlite3
import stat
import sys
import tempfile
from pathlib import Path
from traceback import walk_tb
from unittest.mock import patch
from urllib.parse import unquote, urlsplit

CASE = "tests/test_personal_bootstrap.py::test_cold_real_graph_entry[600895.SS]"
REPO = Path(os.path.abspath(__file__)).parents[1]
ERROR_TYPES = (
    FileNotFoundError, NotADirectoryError, FileExistsError, PermissionError,
    OSError, sqlite3.OperationalError, sqlite3.DatabaseError, ValueError,
    KeyError, TypeError, RuntimeError, TimeoutError,
)
ENVIRONMENT = (
    "SYSTEMROOT", "WINDIR", "SYSTEMDRIVE", "COMSPEC", "PATH", "PATHEXT",
    "PROCESSOR_ARCHITECTURE", "NUMBER_OF_PROCESSORS", "HOME", "USERPROFILE",
    "HOMEDRIVE", "HOMEPATH", "APPDATA", "LOCALAPPDATA", "LOGNAME", "USER",
    "LNAME", "USERNAME", "TEMP", "TMP", "TMPDIR", "PYTEST_DEBUG_TEMPROOT",
)


class OfflineBoundaryStop(BaseException):
    pass


class Boundary:
    def __init__(self):
        self.denied = False
        self.root = None
        self.temp_parent = None
        self.probing = None
        protected = [REPO / name for name in ("config", "data", "logs", "reports")]
        protected.append(Path.home() / ".tradingagents")
        self.protected = tuple(protected)
        self.temp_options = {
            Path(os.path.abspath(value)) for value in tempfile._candidate_tempdir_list()
        }

    def stop(self):
        self.denied = True
        raise OfflineBoundaryStop

    def local(self, path):
        # Inspect each component before following it. The probe never uses remote temp shares.
        if (not path.is_absolute() or not re.fullmatch(r"[A-Za-z]:", path.drive)
                or ctypes.windll.kernel32.GetDriveTypeW(path.anchor) != 3):
            self.stop()
        for component in (*reversed(path.parents), path):
            try:
                info = component.lstat()
            except FileNotFoundError:
                break
            if info.st_file_attributes & stat.FILE_ATTRIBUTE_REPARSE_POINT:
                self.stop()
        return path

    def writable(self, value, event):
        if value is None or isinstance(value, int):
            return
        path = Path(os.path.abspath(os.fsdecode(value)))
        if any(path.is_relative_to(root) for root in self.protected):
            self.stop()
        if self.root is not None and path.is_relative_to(self.root):
            return
        if self.temp_parent is None:
            # tempfile probes its candidate parent with a random file before pytest allocates.
            if event == "open" and path.parent in self.temp_options:
                self.local(path.parent)
                if not path.exists():
                    self.probing = path
                    return
            if event == "os.remove" and path == self.probing:
                return
        elif self.root is None and event == "os.mkdir":
            if path.parent == self.temp_parent and path.name.startswith("pytest-of-"):
                self.local(path)
                return
            if (path.parent.parent == self.temp_parent and path.parent.name.startswith("pytest-of-")
                    and re.fullmatch(r"pytest-[0-9]+", path.name)):
                self.local(path)
                return
        self.stop()

    def audit(self, event, args):
        if event == "socket.bind":
            address = args[1]
            if not isinstance(address, tuple) or address[:2] not in {("127.0.0.1", 0), ("::1", 0)}:
                self.stop()
        elif (event.startswith(("socket.connect", "http.client.", "urllib.", "subprocess.", "os.spawn"))
                or event in {"socket.sendto", "socket.getaddrinfo", "socket.gethostbyname",
                             "socket.gethostbyaddr", "socket.getnameinfo", "os.system", "os.posix_spawn",
                             "os.exec", "os.startfile", "os.startfile/2"}):
            self.stop()
        elif event == "open":
            value, _, flags = args
            if value is None or isinstance(value, int):
                return
            path = Path(os.fsdecode(value))
            name = path.name.casefold()
            if (name.startswith(".env") or name in {".netrc", "_netrc", "credentials", "credentials.json"}
                    or any(part.casefold() in {".aws", ".ssh", ".azure"} for part in path.parts)):
                self.stop()
            write_flags = os.O_WRONLY | os.O_RDWR | os.O_CREAT | os.O_TRUNC | os.O_APPEND
            if flags & write_flags and os.fsdecode(value).casefold() != os.devnull.casefold():
                self.writable(value, event)
        elif event == "sqlite3.connect":
            value = os.fsdecode(args[0])
            if value == ":memory:":
                return
            if value.startswith("file:"):
                parsed = urlsplit(value)
                if parsed.netloc:
                    self.stop()
                value = unquote(parsed.path)
                if re.match(r"^/[A-Za-z]:/", value):
                    value = value[1:]
            self.writable(value, event)
        elif event in {"os.mkdir", "os.remove", "os.rmdir", "os.chmod", "os.utime", "os.truncate"}:
            self.writable(args[0], event)
        elif event in {"os.rename", "os.link", "os.symlink"}:
            for value in args[:2]:
                self.writable(value, event)


@contextlib.contextmanager
def quiet():
    """Discard Python and native output while the diagnostic runs."""
    stdout, stderr = sys.stdout, sys.stderr
    with open(os.devnull, "wb") as discard:
        saved = [os.dup(fd) for fd in (1, 2)]
        try:
            for fd in (1, 2):
                os.dup2(discard.fileno(), fd)
            with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
                yield
        finally:
            stdout.flush()
            stderr.flush()
            for fd, original in enumerate(saved, 1):
                os.dup2(original, fd)
                os.close(original)




def diagnostic_error(error, known_types, path=None):
    def number(name):
        value = getattr(error, name, None)
        return value if type(value) is int and 0 <= value <= 65535 else None

    path_chars = len(os.fspath(path)) if isinstance(path, (str, Path)) else None
    return {
        "exception": type(error).__name__ if type(error) in known_types else "other",
        "errno": number("errno"),
        "winerror": number("winerror"),
        "path_chars": path_chars,
    }


def pytest_setup_operation(error):
    """Classify only observed frames from the installed pytest implementation."""
    tmpdir = sys.modules.get("_pytest.tmpdir")
    paths = sys.modules.get("_pytest.pathlib")
    factory = getattr(tmpdir, "TempPathFactory", None)
    trace = {frame.f_code: lineno for frame, lineno in walk_tb(error.__traceback__)}

    def in_trace(owner, name):
        return getattr(getattr(owner, name, None), "__code__", None) in trace

    def source_line(owner, name):
        code = getattr(getattr(owner, name, None), "__code__", None)
        return linecache.getline(code.co_filename, trace[code]).strip() if code in trace else ""

    base_line = source_line(factory, "getbasetemp")
    if base_line.startswith("temproot = Path(") and base_line.endswith(".resolve()"):
        return "pytest_temp_root_resolution"
    if base_line.startswith("rootdir.mkdir("):
        return "pytest_temp_user_directory_create"
    if in_trace(factory, "getbasetemp"):
        if in_trace(paths, "create_cleanup_lock"):
            return "pytest_temp_cleanup_lock"
        # tmpdir keeps the original allocator while the probe wraps the pathlib alias.
        if in_trace(tmpdir, "make_numbered_dir"):
            if in_trace(paths, "find_prefixed"):
                return "pytest_temp_numbered_directory_scan"
            if source_line(tmpdir, "make_numbered_dir").startswith(("new_path.mkdir(", "raise OSError(")):
                return "pytest_temp_numbered_directory_create"
    if source_line(factory, "mktemp").startswith("p.mkdir("):
        return "pytest_temp_child_directory_create"
    if in_trace(tmpdir, "pytest_sessionfinish") and in_trace(paths, "cleanup_dead_symlinks"):
        return "pytest_temp_session_cleanup"
    return "probe_setup"


def pytest_setup_diagnostic(error):
    return {
        "operation": pytest_setup_operation(error),
        **diagnostic_error(error, ERROR_TYPES, getattr(error, "filename", None)),
    }


def locate_failure(error, bootstrap, store):
    """Match known operations. If the failure site is ambiguous, use the containing stage."""
    history = store.MarketHistoryStore
    known_types = ERROR_TYPES + (bootstrap.PreparationError,)
    # The coordinator explicitly chains sqlite errors. Walk only explicit causes.
    candidates = [error]
    while len(candidates) < 4 and candidates[-1].__cause__ not in (None, *candidates):
        candidates.append(candidates[-1].__cause__)
    for exc in reversed(candidates):
        trace = list(walk_tb(exc.__traceback__))
        failed_path = getattr(exc, "filename", None)
        for frame, lineno in reversed(trace):
            code = frame.f_code
            sqlite_open = code.co_filename == store.__file__ and code.co_name == "open"
            if not sqlite_open and code not in (
                bootstrap.write_local.__code__, bootstrap.identity.__code__,
                history.open_provider_request_authority.__code__, history._claim_payload_root.__code__,
            ):
                continue
            line = linecache.getline(code.co_filename, lineno).strip()
            values = frame.f_locals
            if code is bootstrap.write_local.__code__:
                path = values.get("path")
                label = "identity_registry" if isinstance(path, Path) and path.name.endswith(".registry.json") else "identity_observation"
                if line.startswith("with NamedTemporaryFile("):
                    is_create = any(inner.f_code is tempfile._mkstemp_inner.__code__ for inner, _ in trace)
                    operation = "temporary_create" if is_create else "temporary_context"
                elif line.startswith("os.replace("):
                    operation = "replace"
                    failed_path = getattr(exc, "filename2", None) or path
                elif line.startswith("path.parent.mkdir("):
                    operation = "directory_create"
                    failed_path = failed_path or (path.parent if isinstance(path, Path) else None)
                elif line.startswith(("out.write(", "out.flush(", "os.fsync(")):
                    operation = "temporary_write"
                else:
                    operation = "write_setup"
                return {"operation": f"{label}_{operation}", **diagnostic_error(exc, known_types, failed_path)}
            if code is history._claim_payload_root.__code__:
                operations = (
                    ("payload_root.mkdir(", "authority_owner_directory_create", "payload_root"),
                    ('handle = temporary_owner.open("xb")', "authority_owner_temporary_create", "temporary_owner"),
                    ("os.link(", "authority_owner_link", "owner_path"),
                    ("actual_owner = owner_path.read_bytes(", "authority_owner_read", "owner_path"),
                    ("temporary_owner.unlink(", "authority_owner_cleanup", "temporary_owner"),
                )
                for prefix, operation, variable in operations:
                    if line.startswith(prefix):
                        return {"operation": operation, **diagnostic_error(exc, known_types, values.get(variable))}
                return {"operation": "authority_owner_claim", **diagnostic_error(exc, known_types, failed_path)}
            if sqlite_open and line.startswith("connection = sqlite3.connect("):
                return {"operation": "authority_sqlite_connect", **diagnostic_error(exc, known_types, values.get("database_path"))}
            if code is history.open_provider_request_authority.__code__:
                return {"operation": "authority_store_open", **diagnostic_error(exc, known_types)}
            if code is bootstrap.identity.__code__:
                if line.startswith(("pin = local_path(", "if not pin.is_file()")):
                    operation = "identity_registry_check"
                elif line.startswith("document, listed = project("):
                    operation = "stock_basic_identity_projection"
                else:
                    operation = "stock_basic_identity"
                return {"operation": operation, **diagnostic_error(exc, known_types, failed_path)}
    return {"operation": "stock_basic_unclassified", **diagnostic_error(error, known_types)}



def plugin_for(pytest, boundary, row):
    class Probe:
        def pytest_collection_finish(self, session):
            if [item.nodeid for item in session.items] != [CASE]:
                boundary.stop()

        def pytest_internalerror(self, excinfo):
            if row["operation"] == "probe_setup":
                row.update(pytest_setup_diagnostic(excinfo.value))

        @pytest.hookimpl(hookwrapper=True)
        def pytest_runtest_call(self, item):
            if item.nodeid != CASE:
                boundary.stop()
            from scripts import personal_bootstrap as bootstrap
            from tests.test_personal_bootstrap import SyntheticFeed
            from tradingagents.market_history import store

            hydrate = bootstrap.hydrate
            synthetic_post = SyntheticFeed.post
            post_failure = None

            def observed_post(self, token, endpoint, params, fields, remaining, admit):
                nonlocal post_failure
                try:
                    return synthetic_post(self, token, endpoint, params, fields, remaining, admit)
                except Exception as exc:
                    if endpoint == "stock_basic":
                        post_failure = diagnostic_error(exc, ERROR_TYPES + (bootstrap.PreparationError,))
                    raise

            def observed_hydrate(config, ticker, token, session):
                try:
                    result = hydrate(config, ticker, token, session)
                except Exception as exc:
                    if session.stage != "stock_basic":
                        row["operation"] = "preparation_outside_stock_basic"
                    elif post_failure is None:
                        row.update(locate_failure(exc, bootstrap, store))
                    else:
                        row["operation"] = (
                            "synthetic_stock_basic_after_admission" if session.counts.get("stock_basic")
                            else "synthetic_stock_basic_before_admission"
                        )
                        row.update(post_failure)
                    raise
                row["operation"] = "stock_basic_no_failure_observed"
                return result

            with patch.object(bootstrap, "hydrate", observed_hydrate), patch.object(SyntheticFeed, "post", observed_post):
                yield
    return Probe()


def run_probe(row):
    if (os.name != "nt" or not sys.flags.isolated or not sys.flags.no_site
            or not sys.dont_write_bytecode or len(sys.argv) != 1 or Path.cwd() != REPO):
        raise RuntimeError
    keep = {key: os.environ[key] for key in ENVIRONMENT if key in os.environ}
    os.environ.clear()
    os.environ.update(keep, PYTHON_DOTENV_DISABLED="1", PYTEST_DISABLE_PLUGIN_AUTOLOAD="1",
                      LANGSMITH_TRACING="false", OTEL_SDK_DISABLED="true")
    del keep
    boundary = Boundary()
    boundary.local(REPO)
    boundary.local(Path.home())
    packages = boundary.local(Path(sys.executable).parent.parent / "Lib" / "site-packages")
    if not packages.is_dir():
        raise RuntimeError
    sys.path[:0] = [str(REPO), str(packages)]
    win = sys.getwindowsversion()
    platform._syscmd_ver = lambda *_: ("Windows", "", f"{win.major}.{win.minor}.{win.build}")

    sys.addaudithook(boundary.audit)
    temp_parent = Path(os.path.abspath(os.environ.get("PYTEST_DEBUG_TEMPROOT") or tempfile.gettempdir()))
    boundary.temp_parent = boundary.local(temp_parent).resolve(strict=True)

    import pytest
    from _pytest import pathlib as pytest_paths

    allocate = pytest_paths.make_numbered_dir

    def allocate_owned(root, prefix, mode):
        first = boundary.root is None
        if first:
            if prefix != "pytest-" or root.parent != boundary.temp_parent:
                boundary.stop()
            boundary.local(root)
        path = allocate(root, prefix, mode)
        if first:
            boundary.root = path
        return path

    with (
        patch.object(pytest_paths, "make_numbered_dir", allocate_owned),
        patch.object(pytest_paths, "cleanup_numbered_dir", lambda *_: None),
        patch.object(pytest_paths, "_force_symlink", lambda *_: None),
    ):
        status = int(pytest.main([
            "-c", str(REPO / "pyproject.toml"), "-q", "--tb=no", "--show-capture=no", "--capture=sys",
            "-p", "no:cacheprovider", "-p", "no:logging", "-o", "addopts=", "-o", "tmp_path_retention_policy=all", CASE,
        ], plugins=[plugin_for(pytest, boundary, row)]))
    if boundary.denied:
        raise OfflineBoundaryStop
    if row["operation"] == "probe_setup" and status in (0, 1):
        row["operation"] = "stock_basic_not_observed"
    return status


def main():
    row = {"operation": "probe_setup", "exception": None, "errno": None, "winerror": None, "path_chars": None}
    status = 2
    with quiet():
        try:
            status = run_probe(row)
        except OfflineBoundaryStop:
            row["operation"] = "offline_boundary_stopped"
        except BaseException as exc:
            setup = pytest_setup_diagnostic(exc) if row["operation"] == "probe_setup" else None
            row.update(setup if setup and setup["operation"] != "probe_setup" else diagnostic_error(exc, ERROR_TYPES))
    print(json.dumps(row, separators=(",", ":")))
    return status


if __name__ == "__main__":
    raise SystemExit(main())

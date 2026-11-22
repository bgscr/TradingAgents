"""Run the synthetic current-cache matrix in a bounded, clean-checkout environment.

Usage: python -I -S -B scripts/run_current_cache_compatibility.py
Optional --site-packages selects an existing dependency directory (no install).
Optional --work-dir selects the parent for retained synthetic evidence.
Optional --test-filter selects a focused pytest expression without a matrix manifest.
No network, child process, native qualification or real database is permitted.
"""

import os
import sys
from pathlib import Path

QUALIFICATION_NODE = "tests/test_market_history_persistent_reuse.py::test_current_public_entry_persistent_tushare_hit"
MANIFEST_NAME = "current_cache_compatibility.json"
EVIDENCE_NAME = "current-cache-matrix-evidence.json"
EXPORT_NAME = "current-qualification-export.json"


class QualificationExport:
    """Capture exactly one passing public-entry proof, never a glob-selected fixture."""

    def __init__(self):
        self.captured = None

    def capture(self, nodeid, when, outcome, manifest_path):
        if nodeid.replace("\\", "/") != QUALIFICATION_NODE or when != "call":
            return
        if outcome != "passed":
            raise ValueError("qualification_public_entry_not_passed")
        if self.captured is not None:
            raise ValueError("qualification_duplicate_export")
        import hashlib
        import json

        path = Path(manifest_path)
        raw = path.read_bytes()
        manifest = json.loads(raw)
        if manifest["evidence"]["path"] != EVIDENCE_NAME:
            raise ValueError("qualification_evidence_name")
        evidence = (path.parent / EVIDENCE_NAME).read_bytes()
        if hashlib.sha256(evidence).hexdigest() != manifest["evidence"]["sha256"]:
            raise ValueError("qualification_evidence_digest")
        self.captured = raw, evidence

    def retain(self, root, *, suite_passed):
        import hashlib
        import json

        if suite_passed is not True or self.captured is None:
            raise ValueError("qualification_complete_suite_required")
        root = Path(root)
        if not root.is_dir() or any(
            p.is_symlink() or p.is_junction() for p in (root, *root.parents)
        ):
            raise ValueError("qualification_output_directory")
        raw, evidence = self.captured
        descriptor = {
            "contract": "finite-current-export-v1",
            "scope": "isolated-synthetic-current-cache-only",
            "nodeid": QUALIFICATION_NODE,
            "suite_passed": True,
            "manifest": {"name": MANIFEST_NAME, "sha256": hashlib.sha256(raw).hexdigest()},
            "evidence": {"name": EVIDENCE_NAME, "sha256": hashlib.sha256(evidence).hexdigest()},
        }
        outputs = {
            MANIFEST_NAME: raw,
            EVIDENCE_NAME: evidence,
            EXPORT_NAME: json.dumps(descriptor, sort_keys=True, separators=(",", ":")).encode(),
        }
        if any(
            (root / name).exists() or (root / name).is_symlink() or (root / name).is_junction()
            for name in outputs
        ):
            raise ValueError("qualification_output_exists")
        # Descriptor is last. Partial output cannot be used as a qualified bundle.
        for name, content in outputs.items():
            with (root / name).open("xb") as handle:
                handle.write(content)
                handle.flush()
                os.fsync(handle.fileno())
        return {
            "current_path": str(root / MANIFEST_NAME),
            "current_artifact": {
                key: {"path": str(root / name), "sha256": hashlib.sha256(outputs[name]).hexdigest()}
                for key, name in (
                    ("manifest", MANIFEST_NAME),
                    ("evidence", EVIDENCE_NAME),
                    ("export", EXPORT_NAME),
                )
            },
        }


def qualification_plugin(pytest, export):
    """The actual runner hook is also exercised in a separate synthetic pytest process."""

    class Capture:
        @pytest.hookimpl(hookwrapper=True)
        def pytest_runtest_makereport(self, item, call):
            outcome = yield
            report = outcome.get_result()
            export.capture(
                item.nodeid, report.when, report.outcome, item.funcargs.get("validated_manifest")
            )

    return Capture()


def main():
    if not (sys.flags.isolated and sys.flags.no_site and sys.dont_write_bytecode):
        raise RuntimeError("requires -I -S -B")
    import argparse
    import tempfile

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--site-packages", type=Path)
    parser.add_argument("--work-dir", type=Path)
    parser.add_argument("--test-filter")
    options = parser.parse_args()
    SOURCE = Path(__file__).resolve().parent.parent
    executable_dir = Path(sys.executable).absolute().parent
    environment = (
        executable_dir.parent
        if executable_dir.name.casefold() in {"scripts", "bin"}
        else executable_dir
    )
    SITE = options.site_packages or (
        environment / "Lib/site-packages"
        if os.name == "nt"
        else environment
        / "lib"
        / f"python{sys.version_info.major}.{sys.version_info.minor}"
        / "site-packages"
    )
    SITE = SITE.resolve(strict=True)
    MODE = "focused" if options.test_filter else "matrix"
    ROOT = Path(tempfile.mkdtemp(prefix="synthetic-current-cache-", dir=options.work_dir)).resolve()
    os.environ.clear()  # Never inspect/copy inherited credential values.
    sys.pycache_prefix = str(ROOT / "bytecode")  # Fresh source; -B prevents writing caches.
    os.environ.update(
        HOME=str(ROOT),
        USERPROFILE=str(ROOT),
        TEMP=str(ROOT),
        TMP=str(ROOT),
        TRADINGAGENTS_PROJECT_ROOT=str(ROOT),
        TRADINGAGENTS_CACHE_DIR=str(ROOT / "cache"),
        PYTHONTZPATH=str(SITE / "tzdata/zoneinfo"),
        TZ="UTC",
        PYTEST_DISABLE_PLUGIN_AUTOLOAD="1",
    )
    os.chdir(ROOT)
    # Pytest walks ancestors of absolute test paths. Keep collection entirely in
    # the authorized temporary tree, preserving exact test bytes and relative fixture imports.
    TEST_NAMES = (
        "test_market_history_persistent_reuse.py",
        "test_tushare_pit_provider.py",
        "test_tushare_current_incremental.py",
        "test_tushare_current_acceptance.py",
    )
    (ROOT / "tests").mkdir()
    (ROOT / "scripts").mkdir()
    for name in (*TEST_NAMES, "__init__.py"):
        (ROOT / "tests" / name).write_bytes((SOURCE / "tests" / name).read_bytes())
    (ROOT / "scripts/accept_tushare_current.py").write_bytes(
        (SOURCE / "scripts/accept_tushare_current.py").read_bytes()
    )

    class OfflineBoundaryStop(BaseException):
        pass

    def below(path, root):
        value = os.path.normcase(os.path.realpath(os.path.abspath(os.fspath(path))))
        parent = os.path.normcase(os.path.realpath(os.path.abspath(os.fspath(root))))
        return value == parent or value.startswith(parent + os.sep)

    class Boundary:
        installed = False
        active = None
        site = SITE

        def __init__(self):
            self.denials = []
            self.expected_denials = []
            self.expected = None
            self.connections = set()
            self.connect_count = 0
            self.close_count = 0
            self.optional_imports_denied = []

        def deny(self, category):
            if category == self.expected:
                self.expected_denials.append(category)
            else:
                self.denials.append(category)
            if self.active is not None:
                self.active._stop("offline_" + category)
            raise OfflineBoundaryStop(category) from None

        def check(self, path, writing=False):
            if path is None or isinstance(path, int):
                return
            path = Path(os.fsdecode(path)).absolute()
            if path.name.casefold().startswith(".env") or path.name.casefold() in {
                "credentials",
                "credentials.json",
                "conftest.py",
                "test_pytest_persistence_isolation.py",
                ".netrc",
                "_netrc",
            }:
                self.deny("protected_file")
            if path.name.casefold() == "tradingagents.local.json" and not below(path, ROOT):
                self.deny("protected_file")
            if writing and not below(path, ROOT):
                self.deny("outside_write")
            allowed = (
                ROOT,
                SOURCE / "tradingagents",
                SITE,
                Path(sys.base_prefix),
                Path(sys.prefix),
            )
            exact = {
                SOURCE / "scripts/run_current_cache_compatibility.py",
                SOURCE / "scripts/accept_tushare_current.py",
            }
            exact.update(
                SOURCE / "tests" / name
                for name in (
                    "__init__.py",
                    "test_market_history_persistent_reuse.py",
                    "test_tushare_pit_provider.py",
                    "test_tushare_current_incremental.py",
                    "test_tushare_current_acceptance.py",
                )
            )
            if not writing and not (path in exact or any(below(path, root) for root in allowed)):
                self.deny("outside_read")
            if not below(path, ROOT) and path.suffix.casefold() in {
                ".db",
                ".sqlite",
                ".sqlite3",
                ".sqlite3-wal",
                ".sqlite3-shm",
            }:
                self.deny("real_database")

        def audit(self, event, args):
            if event == "open":
                mode, flags = args[1:3]
                writing = (isinstance(mode, str) and any(c in mode for c in "wax+")) or (
                    isinstance(flags, int)
                    and bool(
                        flags & (os.O_WRONLY | os.O_RDWR | os.O_CREAT | os.O_TRUNC | os.O_APPEND)
                    )
                )
                self.check(args[0], writing)
            elif event.startswith(("socket.", "http.client.", "urllib.")):
                self.deny("network")
            elif event.startswith(
                ("subprocess.", "ctypes.", "winreg.", "os.spawn", "os.posix_spawn")
            ) or event in {"os.system", "os.fork", "os.exec", "os.startfile"}:
                self.deny("process_native_registry")
            elif event == "sqlite3.connect":
                self.database(args[0])
            elif event in {"sqlite3.enable_load_extension", "sqlite3.load_extension"}:
                self.deny("sqlite_extension")
            elif event in {
                "os.remove",
                "os.rmdir",
                "os.mkdir",
                "os.chmod",
                "os.utime",
                "os.truncate",
            }:
                self.check(args[0], True)
            elif event in {"os.rename", "os.link", "os.symlink"}:
                self.check(args[0], True)
                self.check(args[1], True)
            elif event in {"os.listdir", "os.scandir", "os.chdir"}:
                # Import machinery lists the source/package roots, never their data.
                if args[0] not in (str(SOURCE), str(SOURCE / "scripts"), str(SOURCE / "tests")):
                    self.check(args[0])

        def database(self, path):
            if str(path) == ":memory:" or os.fsdecode(path).casefold().startswith("file:"):
                self.deny("unscoped_database")
            self.check(path, True)

    BOUNDARY = Boundary()
    sys.addaudithook(BOUNDARY.audit)
    BOUNDARY.installed = True

    # No application, SDK or third-party package has been imported above this point.
    import _sqlite3
    import re
    import sqlite3

    _connect = sqlite3.connect

    class GuardedConnection(sqlite3.Connection):
        def __init__(self, *args, **kwargs):
            super().__init__(*args, **kwargs)
            self._validated_attach = False
            self.set_authorizer(self.authorize)
            BOUNDARY.connect_count += 1
            BOUNDARY.connections.add(id(self))

        def close(self):
            if id(self) in BOUNDARY.connections:
                BOUNDARY.close_count += 1
                BOUNDARY.connections.remove(id(self))
            return super().close()

        def execute(self, sql, parameters=()):
            attach = re.fullmatch(
                r"\s*ATTACH DATABASE \? AS [A-Za-z_][A-Za-z_0-9]*\s*;?\s*", sql, re.I
            )
            if attach:
                if not isinstance(parameters, (tuple, list)) or len(parameters) != 1:
                    BOUNDARY.deny("unvalidated_attach")
                BOUNDARY.database(parameters[0])
            self._validated_attach = bool(attach)
            try:
                return super().execute(sql, parameters)
            finally:
                self._validated_attach = False

        def authorize(self, action, arg1, arg2, database, trigger):
            if action == sqlite3.SQLITE_FUNCTION and str(arg2).casefold() == "load_extension":
                BOUNDARY.deny("sqlite_extension")
            if action == sqlite3.SQLITE_ATTACH:
                if arg1 is None:
                    if not self._validated_attach:
                        BOUNDARY.deny("unvalidated_attach")
                else:
                    BOUNDARY.database(arg1)
            return sqlite3.SQLITE_OK

    def connect(database, *args, **kwargs):
        BOUNDARY.database(database)
        if len(args) > 4 or "factory" in kwargs:
            BOUNDARY.deny("custom_sqlite_connection")
        kwargs.update(factory=GuardedConnection, cached_statements=0)
        return _connect(database, *args, **kwargs)

    sqlite3.connect = sqlite3.dbapi2.connect = _sqlite3.connect = connect
    import socket

    socket.has_ipv6 = False  # Suppress urllib3's optional local socket capability probe.
    import platform

    platform.machine = lambda: "AMD64"
    platform.processor = lambda: "AMD64"
    platform.system = lambda: "Windows" if os.name == "nt" else "Linux"
    platform.platform = lambda *args, **kwargs: "synthetic-offline"
    platform.win32_ver = lambda *args, **kwargs: ("10", "synthetic", "", "Multiprocessor Free")
    import types

    # Only non-target import fanout is replaced. Target modules below load fully.
    yahoo = types.ModuleType("yfinance")
    yahoo.__path__ = []

    def forbidden_vendor(*args, **kwargs):
        BOUNDARY.deny("other_provider")

    def yahoo_attribute(name):
        if name.startswith("__"):
            raise AttributeError(name)
        return forbidden_vendor

    yahoo.__getattr__ = yahoo_attribute
    sys.modules["yfinance"] = yahoo
    yahoo_errors = types.ModuleType("yfinance.exceptions")

    class YFRateLimitError(Exception):
        pass

    yahoo_errors.YFRateLimitError = YFRateLimitError
    sys.modules[yahoo_errors.__name__] = yahoo_errors
    for vendor_name in ("baostock", "akshare"):
        vendor = types.ModuleType(vendor_name)
        vendor.__path__ = []
        vendor.__getattr__ = yahoo_attribute  # Every unpatched operation is fatal.
        sys.modules[vendor_name] = vendor
    agents = types.ModuleType("tradingagents.agents")
    agents.__path__ = [str(SOURCE / "tradingagents/agents")]
    sys.modules[agents.__name__] = agents  # Avoid model constructors; real schemas still import.

    # dateutil Windows-local-zone registry access is irrelevant to fixed UTC time.
    from datetime import timedelta, tzinfo
    from zoneinfo import ZoneInfo

    tzwin = types.ModuleType("dateutil.tz.win")

    class FixedUTC(tzinfo):
        def utcoffset(self, dt):
            return timedelta(0)

        def dst(self, dt):
            return timedelta(0)

        def tzname(self, dt):
            return "UTC"

    tzwin.tzwin, tzwin.tzwinlocal = ZoneInfo, FixedUTC
    sys.modules[tzwin.__name__] = tzwin

    class ImportBoundary:
        def find_spec(self, fullname, path=None, target=None):
            top = fullname.split(".")[0]
            if top in {"dotenv", "ctypes", "_ctypes"}:
                BOUNDARY.optional_imports_denied.append(fullname)
                raise ImportError("optional import disabled in offline data launcher")
            # numpy._core._internal explicitly supports ctypes=None on ImportError.
            # Disable that optional interface; do not allow its kernel32 loader.
            # Real run_telemetry requires langchain_core callbacks/messages/outputs;
            # permit that dependency while denying model providers and all transport.
            if top in {
                "conftest",
                "tushare",
                "baostock",
                "akshare",
                "curl_cffi",
                "pycurl",
                "openai",
                "anthropic",
                "langgraph",
                "langchain",
            }:
                BOUNDARY.deny("forbidden_import")

    sys.meta_path.insert(0, ImportBoundary())
    sys.path[:0] = [str(ROOT), str(SOURCE), str(SITE)]

    import json

    status = 99
    summary = {"tests_run": 0, "failures": 0, "errors": 0, "setup_error": None}
    try:
        # Load real NumPy using its supported ctypes=None fallback first. Pandas
        # errors/__init__.py then needs only an importable ctypes name for its
        # unused PyperclipWindowsException formatter. Expose no native operations.
        import numpy

        summary["numpy_version"] = numpy.__version__
        unavailable_ctypes = types.ModuleType("ctypes")

        def unavailable_native(*args, **kwargs):
            BOUNDARY.deny("native_interface_disabled")

        def unavailable_ctypes_attribute(name):
            if name.startswith("__"):
                raise AttributeError(name)
            raise ImportError("native interface disabled")

        unavailable_ctypes.WinError = unavailable_native
        unavailable_ctypes.__getattr__ = unavailable_ctypes_attribute
        sys.modules["ctypes"] = unavailable_ctypes
        # Report/terminal code imports only this annotation from the real lazy
        # runnables package. Avoid importing asyncio's Windows native transport;
        # no runnable execution or model behavior is replaced or permitted.
        import langchain_core.runnables as runnables

        class AnnotationOnlyRunnableConfig:
            def __new__(cls, *args, **kwargs):
                BOUNDARY.deny("runnable_config_execution")

        runnables.RunnableConfig = AnnotationOnlyRunnableConfig
        summary["annotation_only_overrides"] = ["langchain_core.runnables.RunnableConfig"]
        import pytest

        (ROOT / "pytest.ini").write_text("[pytest]\n", encoding="utf-8")

        export = QualificationExport()

        class Report:
            def pytest_runtest_logstart(self, nodeid, location):
                summary["tests_run"] += 1

            def pytest_runtest_logreport(self, report):
                if report.failed:
                    key = "failures" if report.when == "call" else "errors"
                    summary[key] += 1

            def pytest_collectreport(self, report):
                if report.failed:
                    summary["errors"] += 1

            def pytest_sessionfinish(self, session, exitstatus):
                summary["tests_collected"] = session.testscollected

        status = int(
            pytest.main(
                [
                    str(ROOT / "tests/test_market_history_persistent_reuse.py"),
                    str(ROOT / "tests/test_tushare_pit_provider.py"),
                    str(ROOT / "tests/test_tushare_current_incremental.py"),
                    str(ROOT / "tests/test_tushare_current_acceptance.py"),
                    "-c",
                    str(ROOT / "pytest.ini"),
                    "--noconftest",
                    "-p",
                    "no:cacheprovider",
                    "--basetemp",
                    str(ROOT / "pytest"),
                    "--log-file",
                    str(ROOT / "pytest.log"),
                    "-q",
                    "-s",
                    "--tb=short",
                    "-o",
                    "addopts=",
                    "-o",
                    "testpaths=",
                    "-o",
                    "pythonpath=",
                    "--rootdir",
                    str(ROOT),
                    "-x",
                ]
                + (["-k", options.test_filter] if options.test_filter else []),
                plugins=[Report(), qualification_plugin(pytest, export)],
            )
        )
        if status == 0 and MODE == "matrix":
            retained = export.retain(
                ROOT, suite_passed=not BOUNDARY.denials and not BOUNDARY.connections
            )
            summary["manifest"] = retained["current_path"]
            summary["current_artifact"] = retained["current_artifact"]

    except BaseException as exc:
        status = 1
        summary["setup_error"] = type(exc).__name__
        frames = []
        trace = exc.__traceback__
        while trace is not None:
            frames.append(
                {
                    "file": trace.tb_frame.f_code.co_filename,
                    "line": trace.tb_lineno,
                    "function": trace.tb_frame.f_code.co_name,
                }
            )
            trace = trace.tb_next
        summary["setup_frames"] = frames  # No arguments, locals or exception text.
    finally:
        if BOUNDARY.denials or BOUNDARY.connections:
            status = 1
        summary.update(
            exitstatus=status,
            unexpected_denials=BOUNDARY.denials,
            expected_denials=BOUNDARY.expected_denials,
            optional_imports_denied=BOUNDARY.optional_imports_denied,
            ctypes_interface="NumPy real fallback; Pandas clipboard WinError blocked; no native functions",
            sqlite_connections_opened=BOUNDARY.connect_count,
            sqlite_connections_closed=BOUNDARY.close_count,
            sqlite_connections_remaining=len(BOUNDARY.connections),
            synthetic_root=str(ROOT),
            live_network_transport="absent; all socket/HTTP audit events denied",
            tls_scope="synthetic configuration only; no TLS handshake",
            suite=MODE,
            conftest_loaded=any(name.endswith("conftest") for name in sys.modules),
            full_target_modules={
                name: getattr(module, "__file__", None)
                for name, module in sys.modules.items()
                if name
                in {
                    "tradingagents.dataflows.market_snapshot",
                    "tradingagents.dataflows.tushare_market_history",
                    "tradingagents.picker.tushare_provider",
                    "tradingagents.market_history.coordinator",
                    "tradingagents.market_history.store",
                    "tradingagents.tushare_https",
                }
            },
        )
        output = ROOT / "boundary-result.json"
        output.write_text(json.dumps(summary, indent=2), encoding="utf-8")
        print("BOUNDARY_RESULT=" + str(output))
        print(json.dumps(summary))
    raise SystemExit(status)


if __name__ == "__main__":
    main()

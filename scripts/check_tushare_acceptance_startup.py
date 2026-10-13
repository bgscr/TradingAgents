"""Cold acceptance bootstrap regression, with no DB, credentials or sends.

Run with -I -S -B. Calls the launcher's actual application_runtime, never main.
Platform functions and NumPy/Pandas are not preloaded or stubbed. Only off-path
vendors/model packages and optional native/timezone interfaces are unavailable.
This Python tripwire is not native/protocol qualification.
"""
import os
import sys


def main():
    if not (sys.flags.isolated and sys.flags.no_site and sys.dont_write_bytecode):
        raise SystemExit("Requires -I -S -B")
    import argparse
    import importlib.util
    import json
    import tempfile
    import types
    from datetime import datetime, timedelta, tzinfo
    from pathlib import Path

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--work-dir", type=Path)
    parser.add_argument("--site-packages", type=Path)
    options = parser.parse_args()
    source = Path(__file__).resolve().parents[1]
    executable_dir = Path(sys.executable).absolute().parent
    environment = executable_dir.parent if executable_dir.name.casefold() in {"scripts", "bin"} else executable_dir
    site = options.site_packages or (environment / "Lib/site-packages" if os.name == "nt" else
        environment / "lib" / f"python{sys.version_info.major}.{sys.version_info.minor}" / "site-packages")
    site = site.resolve(strict=True)
    root = Path(tempfile.mkdtemp(prefix="synthetic-acceptance-startup-", dir=options.work_dir)).resolve()
    os.environ.clear()
    os.environ.update(HOME=str(root), USERPROFILE=str(root), TEMP=str(root), TMP=str(root),
                      TRADINGAGENTS_PROJECT_ROOT=str(root), TZ="UTC", PYTEST_DISABLE_PLUGIN_AUTOLOAD="1")
    os.chdir(root)
    sys.pycache_prefix = str(root / "unused-bytecode")

    def load(name, path):
        spec = importlib.util.spec_from_file_location(name, path)
        module = importlib.util.module_from_spec(spec)
        sys.modules[name] = module
        spec.loader.exec_module(module)
        return module

    # Exact stdlib-only support definitions, without importing the package.
    support = load("_cold_acceptance_support", source / "tradingagents/tushare_acceptance.py")
    guard = support.RequestGuard("2026-09-12", "2026-10-12", lambda value: None,
                                 now=lambda: 0.0, sleep=lambda seconds: None)
    denials, audited, optional = [], [], []

    class BoundaryStop(BaseException):
        pass

    def deny(code):
        denials.append(code)
        raise BoundaryStop(code)

    def below(path, parent):
        value, parent = os.path.normcase(os.path.realpath(path)), os.path.normcase(os.path.realpath(parent))
        return value == parent or value.startswith(parent + os.sep)

    def check_read(path):
        if isinstance(path, int):
            return
        path = Path(os.fsdecode(path)).absolute()
        name = path.name.casefold()
        if path == root / "tradingagents.local.json":
            raise FileNotFoundError("Synthetic profile absent")
        if (name.startswith(".env") or name in {"credentials", "credentials.json", ".netrc", "_netrc",
                "tradingagents.local.json", "conftest.py", "test_pytest_persistence_isolation.py"}
                or ".sqlite" in name or path.suffix.casefold() == ".db"):
            deny("protected_read")
        exact = {source / "scripts/accept_tushare_current.py", Path(__file__).resolve()}
        if path not in exact and not any(below(path, parent) for parent in (
                root, source / "tradingagents", site, Path(sys.base_prefix))):
            deny("outside_read")

    def audit(event, args):
        if event.startswith(("socket.", "http.client.", "urllib.")):
            audited.append(event if event in support.AUDIT_NAMES else "other_audit_event")
            guard.audit(event, args)
            deny("network_guard_did_not_stop")
        elif event == "open":
            path, access, flags = args[:3]
            if ((isinstance(access, str) and any(c in access for c in "wax+")) or
                    isinstance(flags, int) and flags & (os.O_WRONLY | os.O_RDWR | os.O_CREAT | os.O_TRUNC | os.O_APPEND)):
                deny("write")
            check_read(path)
        elif event.startswith(("subprocess.", "ctypes.", "winreg.", "os.spawn", "os.posix_spawn")) or event in {
                "os.system", "os.fork", "os.exec", "os.startfile", "sqlite3.connect",
                "sqlite3.enable_load_extension", "sqlite3.load_extension"}:
            deny("process_native_registry_database")
        elif event in {"os.remove", "os.rmdir", "os.mkdir", "os.rename", "os.link", "os.symlink",
                       "os.chmod", "os.utime", "os.truncate"}:
            deny("filesystem_mutation")
        elif event in {"os.listdir", "os.scandir", "os.chdir"} and args[0] != str(source):
            check_read(args[0])

    sys.addaudithook(audit)
    # Protection precedes every project package / third-party import.
    unavailable_ctypes = types.ModuleType("ctypes")

    def unavailable_native(*args, **kwargs):
        deny("native_interface")

    def unavailable_attribute(name):
        if name.startswith("__"):
            raise AttributeError(name)
        raise ImportError("Optional native interface disabled")

    unavailable_ctypes.WinError = unavailable_native
    unavailable_ctypes.__getattr__ = unavailable_attribute

    class ImportBoundary:
        def find_spec(self, fullname, path=None, target=None):
            if fullname == "pandas.errors":
                # Unused Windows exception formatter needs an importable name,
                # after NumPy has selected its supported ctypes=None fallback.
                sys.modules["ctypes"] = unavailable_ctypes
            if fullname.split(".")[0] in {"dotenv", "ctypes", "_ctypes"}:
                optional.append(fullname)
                raise ImportError("Optional interface disabled")
            if fullname.split(".")[0] in {"conftest", "tushare", "baostock", "akshare", "curl_cffi",
                    "pycurl", "openai", "anthropic", "langgraph", "langchain"}:
                deny("forbidden_import")

    sys.meta_path.insert(0, ImportBoundary())
    yahoo = types.ModuleType("yfinance")
    yahoo.__path__ = []

    def vendor_attribute(name):
        if name.startswith("__"):
            raise AttributeError(name)
        deny("other_provider")

    yahoo.__getattr__ = vendor_attribute
    sys.modules["yfinance"] = yahoo
    yahoo_errors = types.ModuleType("yfinance.exceptions")
    yahoo_errors.YFRateLimitError = type("YFRateLimitError", (Exception,), {})
    sys.modules[yahoo_errors.__name__] = yahoo_errors
    agents = types.ModuleType("tradingagents.agents")
    agents.__path__ = [str(source / "tradingagents/agents")]
    sys.modules[agents.__name__] = agents  # Real schemas; no model factory fanout.
    tzwin = types.ModuleType("dateutil.tz.win")

    class FixedUTC(tzinfo):
        def utcoffset(self, dt):
            return timedelta(0)

        def dst(self, dt):
            return timedelta(0)

        def tzname(self, dt):
            return "UTC"

    from zoneinfo import ZoneInfo
    tzwin.tzwin, tzwin.tzwinlocal = ZoneInfo, FixedUTC
    sys.modules[tzwin.__name__] = tzwin
    sys.path[:0] = [str(source), str(site)]
    summary = {"scope": "cold-application-bootstrap-only", "root": str(root), "completed": False}
    status = 1
    try:
        import platform
        import socket

        from dateutil.tz import tz as dateutil_tz

        # Pandas 2.x resolves UTC while importing its timezone extensions. Use
        # dateutil's own bundled zonefile, never /etc/localtime or system TZPATHS.
        # Exercise the same file lookup on Windows too; no broader read permission.
        dateutil_tz.TZPATHS = ()
        dateutil_tz.TZFILES = ()
        dateutil_tz.tzwin = None
        fixed_utc = dateutil_tz.gettz.nocache("UTC")
        assert isinstance(fixed_utc, dateutil_tz.tzfile)
        assert fixed_utc.utcoffset(datetime(2026, 10, 13, tzinfo=fixed_utc)) == timedelta(0)
        assert platform._uname_cache is None and not platform._platform_cache
        assert not any(name in sys.modules for name in ("numpy", "pandas", "tradingagents"))
        public_functions = tuple(getattr(platform, name) for name in ("machine", "system", "processor", "platform"))
        assert all(function.__module__ == "platform" for function in public_functions)
        before = platform.uname, platform.win32_ver, platform._platform_cache, socket.has_ipv6
        launcher = load("_cold_acceptance_launcher", source / "scripts/accept_tushare_current.py")
        with launcher.application_runtime(support, guard) as modules:
            assert len(modules) == 6
            assert "pandas" in sys.modules and "numpy" in sys.modules
            assert public_functions == tuple(getattr(platform, name) for name in ("machine", "system", "processor", "platform"))
            if sys.platform == "win32":
                assert platform.machine() in {"x86", "AMD64", "ARM", "ARM64"}
                assert platform.system() == "Windows" and platform.processor() == ""
                assert platform.node() == "" and platform.platform().startswith("Windows")
            assert not audited and guard.attempts == 0 and guard.stopped is None
        assert (platform.uname, platform.win32_ver, platform._platform_cache, socket.has_ipv6) == before
        # Synthetic audit event: no hostname lookup. Successful imports must not
        # grant even this previously forbidden operation any new permission.
        try:
            sys.audit("socket.gethostname")
        except support.AcceptanceStop:
            assert guard.rejection == {"audit_event": "socket.gethostname", "stage": "application_imports"}
        else:
            raise AssertionError("guard did not stop")
        assert audited == ["socket.gethostname"] and not denials and guard.attempts == 0
        summary.update(completed=True, numpy=sys.modules["numpy"].__version__,
                       pandas=sys.modules["pandas"].__version__, platform_functions_unstubbed=True,
                       timezone="dateutil bundled UTC; system search disabled",
                       restoration_checked=True, post_import_guard_rejection=guard.rejection)
        status = 0
    except BaseException as exc:
        summary["failure_type"] = type(exc).__name__
        frames, trace = [], exc.__traceback__
        while trace is not None:
            frames.append({"file": trace.tb_frame.f_code.co_filename, "line": trace.tb_lineno,
                           "function": trace.tb_frame.f_code.co_name})
            trace = trace.tb_next
        summary["frames"] = frames  # No exception text, arguments or locals.
    summary.update(audit_events=audited, denials=denials, optional_imports_denied=optional,
                   physical_attempts=guard.attempts, writes="forbidden", databases="forbidden",
                   pytest_loaded="pytest" in sys.modules, conftest_loaded="conftest" in sys.modules)
    print(json.dumps(summary, indent=2))
    return status


if __name__ == "__main__":
    raise SystemExit(main())

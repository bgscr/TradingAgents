"""Process-local guard installed before application imports in diagnostics.

Defense in depth for trusted diagnostic code, not an OS security sandbox.
"""

from __future__ import annotations

import ctypes  # noqa: F401 -- initialize interpreter DLL bindings before the audit guard
import os
import platform
import sys
from collections.abc import MutableMapping
from pathlib import Path


def safe_environment(root):
    root = Path(root).resolve()
    result = {key: os.environ[key] for key in ("SYSTEMROOT", "WINDIR") if key in os.environ}
    result.update(
        {
            "TEMP": str(root),
            "TMP": str(root),
            "TMPDIR": str(root),
            "HOME": str(root),
            "USERPROFILE": str(root),
            "PYTHON_DOTENV_DISABLED": "1",
            "PYTEST_DISABLE_PLUGIN_AUTOLOAD": "1",
            "TRADINGAGENTS_PROJECT_ROOT": str(root),
            "TRADINGAGENTS_RESULTS_DIR": str(root / "results"),
            "TRADINGAGENTS_CACHE_DIR": str(root / "cache"),
            "TRADINGAGENTS_MEMORY_LOG_PATH": str(root / "memory.jsonl"),
            "WIN_PD_OVERRIDE_LOCAL_APPDATA": str(root / "local-app-data"),
            "WIN_PD_OVERRIDE_APPDATA": str(root / "app-data"),
            "WIN_PD_OVERRIDE_COMMON_APPDATA": str(root / "common-app-data"),
        }
    )
    return result


class DeniedAccess(RuntimeError):
    pass


def install(root, source, *, offline=True):
    if not (sys.flags.isolated and sys.flags.no_site and sys.dont_write_bytecode):
        raise RuntimeError("diagnostics require -I -S -B")
    root, source = Path(root).resolve(), Path(source).resolve()
    counts = {"network": 0, "credentials": 0, "subprocess": 0, "outside_write": 0}
    blocked_network = []
    clean = safe_environment(root)
    os.environ.clear()
    os.environ.update(clean)

    def sensitive(key):
        return any(
            part in str(key).upper()
            for part in ("TOKEN", "API_KEY", "SECRET", "PASSWORD", "CREDENTIAL")
        )

    class Environment(MutableMapping):
        def __getitem__(self, key):
            if sensitive(key):
                counts["credentials"] += 1
                raise DeniedAccess("credential environment access prohibited")
            return clean[key]

        def __setitem__(self, key, value):
            if sensitive(key):
                raise DeniedAccess("credential environment mutation prohibited")
            clean[key] = value

        def __delitem__(self, key):
            del clean[key]

        def __iter__(self):
            return iter(clean)

        def __len__(self):
            return len(clean)

    os.environ = Environment()  # noqa: B003 -- native environment was cleared above
    if sys.platform == "win32":
        version = sys.getwindowsversion()
        platform.machine = lambda: "AMD64"
        platform.processor = lambda: "AMD64"
        platform.system = lambda: "Windows"
        platform.node = lambda: "isolated-diagnostic"
        platform.release = lambda: str(version.major)
        platform.version = lambda: f"{version.major}.{version.minor}.{version.build}"
        platform.win32_ver = lambda *a, **k: (str(version.major), platform.version(), "", "")
        sys.modules["dateutil.tz.win"] = None
    dependencies = Path(sys.executable).resolve().parents[1] / "Lib" / "site-packages"
    if not dependencies.is_dir():
        # POSIX venv, without executing site.py or .pth startup code.
        dependencies = (
            Path(sys.prefix)
            / "lib"
            / f"python{sys.version_info.major}.{sys.version_info.minor}"
            / "site-packages"
        )
    sys.path[:0] = [str(source), str(dependencies)]
    read_roots = (root, source, dependencies, Path(sys.base_prefix).resolve())

    def write(path):
        if isinstance(path, (str, bytes, os.PathLike)) and not Path(
            os.fsdecode(path)
        ).resolve().is_relative_to(root):
            counts["outside_write"] += 1
            raise DeniedAccess("diagnostic write outside isolated output")

    def audit(event, args):
        if event.startswith("winreg."):
            raise DeniedAccess("registry access prohibited")
        if event in {"subprocess.Popen", "os.system", "os.posix_spawn"}:
            counts["subprocess"] += 1
            raise DeniedAccess("worker subprocess prohibited")
        if offline and event in {
            "socket.connect",
            "socket.getaddrinfo",
            "socket.bind",
            "socket.sendto",
        }:
            counts["network"] += 1
            frame = sys._getframe(1)
            blocked_network.append(
                {
                    "event": event,
                    "module": Path(frame.f_code.co_filename).name,
                    "function": frame.f_code.co_name,
                }
            )
            raise DeniedAccess("offline network prohibited")
        if event == "ctypes.dlopen" and Path(str(args[0])).stem.casefold() in {
            "kernel32",
            "advapi32",
            "webauthn",
            "ncrypt",
            "secur32",
            "user32",
        }:
            frame = sys._getframe(1)
            while frame:
                if frame.f_code.co_filename.replace("\\", "/").endswith(
                    ("/colorama/win32.py", "/rich/_win32_console.py")
                ):
                    break
                frame = frame.f_back
            else:
                raise DeniedAccess("native security APIs prohibited")
        if event == "sqlite3.connect" and str(args[0]) != ":memory:":
            write(args[0])
        if event == "open":
            path, mode, flags = args
            if isinstance(path, (str, bytes, os.PathLike)):
                actual = Path(os.fsdecode(path)).resolve()
                if (
                    actual.name.startswith(".env")
                    or actual.name == "test_pytest_persistence_isolation.py"
                    or any(part.lower() in {".ssh", ".aws", ".azure"} for part in actual.parts)
                ):
                    counts["credentials"] += 1
                    raise DeniedAccess("protected file access prohibited")
                if not any(actual.is_relative_to(allowed) for allowed in read_roots):
                    raise DeniedAccess("read outside diagnostic source/runtime")
            if (isinstance(mode, str) and any(c in mode for c in "wax+")) or flags & (
                os.O_WRONLY | os.O_RDWR | os.O_CREAT | os.O_TRUNC | os.O_APPEND
            ):
                write(path)
        if event in {"os.mkdir", "os.remove", "os.rmdir", "os.chmod", "os.utime", "os.truncate"}:
            write(args[0])
        if event in {"os.rename", "os.link", "os.symlink"}:
            write(args[0])
            write(args[1])

    sys.addaudithook(audit)
    if offline:
        # curl_cffi's native transport does not emit Python socket audit events.
        # Seal that path before application/AKShare imports.
        from curl_cffi import requests as curl_requests

        def no_curl(*args, **kwargs):
            counts["network"] += 1
            raise DeniedAccess("offline native HTTP prohibited")

        curl_requests.Session.request = no_curl
        curl_requests.AsyncSession.request = no_curl
    return counts, blocked_network

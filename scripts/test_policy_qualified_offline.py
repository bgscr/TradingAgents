"""Bounded offline regression entrypoint; never activate an editable environment.

Run with an existing compatible interpreter using -I -B -S. Dependencies are
read from an explicit existing directory without processing .pth/site hooks.
All writes and synthetic SQLite claims are confined to a fresh scratch root.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import traceback
from pathlib import Path


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--dependencies", type=Path, required=True)
    parser.add_argument("--scratch-root", type=Path, required=True)
    parser.add_argument("tests", nargs="+")
    args = parser.parse_args()
    if not (sys.flags.isolated and sys.flags.no_site and sys.dont_write_bytecode):
        raise RuntimeError("offline runner requires -I -B -S")
    source = Path(__file__).resolve().parents[1]
    dependencies = args.dependencies.resolve(strict=True)
    scratch = args.scratch_root.resolve(strict=False)
    if (
        scratch == source
        or scratch.is_relative_to(source)
        or not scratch.name.startswith("offline-")
    ):
        raise ValueError("a fresh external offline-* scratch directory is required")
    scratch.mkdir(exist_ok=False)
    for name in ("profile", "temp", "initial-runtime"):
        (scratch / name).mkdir()
    for name in list(os.environ):
        upper = name.upper()
        if (
            any(part in upper for part in ("API_KEY", "API_SECRET", "ACCESS_TOKEN", "AUTH_TOKEN"))
            or upper.endswith("_TOKEN")
            or upper.startswith(("RUN_LIVE_", "TRADINGAGENTS_"))
            or upper in {"PYTHONPATH", "PYTHONHOME", "PYTEST_ADDOPTS", "PYTEST_PLUGINS"}
        ):
            os.environ.pop(name, None)
    os.environ.update(
        {
            "PYTEST_DISABLE_PLUGIN_AUTOLOAD": "1",
            "PYTHON_DOTENV_DISABLED": "1",
            "RUN_LIVE_PROVIDER_TESTS": "0",
            "RUN_LIVE_DEEPSEEK_TESTS": "0",
            "RUN_LIVE_TUSHARE_TESTS": "0",
            "USERPROFILE": str(scratch / "profile"),
            "TMP": str(scratch / "temp"),
            "TEMP": str(scratch / "temp"),
            "TRADINGAGENTS_PROJECT_ROOT": str(scratch / "initial-runtime"),
            "TRADINGAGENTS_RESULTS_DIR": str(scratch / "initial-runtime" / "results"),
            "TRADINGAGENTS_CACHE_DIR": str(scratch / "initial-runtime" / "cache"),
            "TRADINGAGENTS_MEMORY_LOG_PATH": str(scratch / "initial-runtime" / "memory.json"),
            "TRADINGAGENTS_MARKET_HISTORY_DATABASE_PATH": str(
                scratch / "initial-runtime" / "market.db"
            ),
            "TRADINGAGENTS_MARKET_HISTORY_PAYLOAD_ROOT": str(
                scratch / "initial-runtime" / "payloads"
            ),
            "TRADINGAGENTS_MARKET_HISTORY_BACKUP_ROOT": str(
                scratch / "initial-runtime" / "backups"
            ),
        }
    )
    denied: list[dict] = []

    def writable(value: object) -> None:
        if isinstance(value, int):
            return
        path = Path(os.fsdecode(value)).resolve(strict=False)
        if path != scratch and not path.is_relative_to(scratch):
            raise RuntimeError("offline guard: write outside synthetic scratch")

    def audit(event: str, values: tuple) -> None:
        if event in {
            "subprocess.Popen",
            "os.system",
            "os.startfile",
            "os.startfile/2",
            "socket.connect",
            "socket.bind",
            "socket.getaddrinfo",
            "socket.gethostbyname",
        }:
            denied.append({"event": event, "disposition": "BLOCKED_BEFORE_OPERATION",
                           "stack": [{"file": frame.filename, "line": frame.lineno,
                                      "function": frame.name}
                                     for frame in traceback.extract_stack(limit=6)[:-1]]})
            raise RuntimeError("offline guard: forbidden process/network operation " + event)
        if event == "open":
            path, _mode, flags = values
            if flags & (os.O_WRONLY | os.O_RDWR | os.O_CREAT | os.O_TRUNC | os.O_APPEND):
                writable(path)
        elif event in {"os.mkdir", "os.remove", "os.rmdir", "os.chmod", "os.truncate", "os.utime"}:
            writable(values[0])
        elif event in {"os.rename", "os.link", "os.symlink"}:
            writable(values[0])
            writable(values[1])
        elif event == "sqlite3.connect":
            target = str(values[0])
            if target != ":memory:":
                from urllib.parse import unquote, urlparse

                if target.startswith("file:"):
                    target = unquote(urlparse(target).path).lstrip("/")
                writable(target)

    sys.addaudithook(audit)
    sys.path[:0] = [str(source), str(dependencies)]
    import pytest

    receipt = {
        "label": "SYNTHETIC_OFFLINE_TESTS_ONLY",
        "source": str(source),
        "python": sys.version,
        "interpreter": sys.executable,
        "site_disabled": bool(sys.flags.no_site),
        "editable_hooks_loaded": any("__editable__" in name for name in sys.modules),
        "dependencies": str(dependencies),
        "selectors": args.tests,
        "real_approval": False,
        "real_claim": False,
        "native_launch": False,
    }
    (scratch / "environment.json").write_text(
        json.dumps(receipt, indent=2) + "\n", encoding="utf-8"
    )
    code = pytest.main(
        [
            "-q",
            "-p",
            "no:cacheprovider",
            "--basetemp",
            str(scratch / "pytest-temp"),
            "--log-file",
            str(scratch / "pytest.log"),
            "--junitxml",
            str(scratch / "junit.xml"),
            *args.tests,
        ]
    )
    (scratch / "result.json").write_text(
        json.dumps({**receipt, "exit_code": int(code), "guard_denials": denied}, indent=2) + "\n",
        encoding="utf-8",
    )
    return int(code)


if __name__ == "__main__":
    raise SystemExit(main())

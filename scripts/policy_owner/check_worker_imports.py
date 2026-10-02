"""Check the pinned worker's import closure without opening owner state.

No service, authentication, provider, database, qualification or application is
started. All filesystem writes and process/network/database operations are denied
inside this fresh interpreter before importing the fixed validation modules.
"""

from __future__ import annotations

import importlib
import importlib.machinery
import json
import os
import sys
import types
from pathlib import Path

WORKER_MODULES = (
    "tradingagents.policy_owner_admission_crypto",
    "tradingagents.policy_owner_admission_v2",
    "tradingagents.policy_owner_admission_windows",
    "tradingagents.policy_owner_admission_migration",
)


def check(root: Path) -> dict:
    root = root.resolve(strict=True)
    if "tradingagents" in sys.modules:
        raise ValueError("namespace was already initialized")
    package = types.ModuleType("tradingagents")
    package.__path__ = [str(root / "source" / "tradingagents")]
    package.__package__ = "tradingagents"
    package.__spec__ = importlib.machinery.ModuleSpec("tradingagents", None, is_package=True)
    sys.modules["tradingagents"] = package

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
            "sqlite3.connect",
            "os.mkdir",
            "os.remove",
            "os.rmdir",
            "os.rename",
            "os.link",
            "os.symlink",
            "os.chmod",
            "os.utime",
            "os.truncate",
        }:
            raise RuntimeError("worker import check forbids effects: " + event)
        if event == "open" and values[2] & (
            os.O_WRONLY | os.O_RDWR | os.O_CREAT | os.O_TRUNC | os.O_APPEND
        ):
            raise RuntimeError("worker import check forbids writes")

    sys.addaudithook(audit)
    observations = []
    for name in WORKER_MODULES:
        try:
            origin = root / "source" / "tradingagents" / (name.rsplit(".", 1)[1] + ".py")
            raw = origin.read_bytes()
            module = types.ModuleType(name)
            module.__file__ = str(origin)
            module.__package__ = "tradingagents"
            module.__spec__ = importlib.machinery.ModuleSpec(name, None, origin=str(origin))
            sys.modules[name] = module
            exec(compile(raw, str(origin), "exec", dont_inherit=True), module.__dict__)
            origin = Path(module.__file__).resolve(strict=True)
            if not origin.is_relative_to(root / "source"):
                raise ValueError("validation module escaped the exact worker depot")
            observations.append({"module": name, "imported": True, "origin": str(origin)})
        except Exception as error:
            observations.append(
                {
                    "module": name,
                    "imported": False,
                    "error_type": type(error).__name__,
                    "error": str(error),
                }
            )
    return {
        "contract_version": "policy-owner-admission-worker-import-check-v2",
        "interpreter": sys.executable,
        "depot": str(root),
        "modules": observations,
        "passed": all(item["imported"] for item in observations),
        "owner_instantiated": False,
        "authentication_executed": False,
        "qualification_executed": False,
        "execution_authorized": False,
    }


def main() -> int:
    if not (sys.flags.isolated and sys.flags.no_site and sys.dont_write_bytecode):
        raise ValueError("worker import check requires -I -B -S")
    if len(sys.argv) != 2:
        raise ValueError("one exact staged or registered depot path is required")
    report = check(Path(sys.argv[1]))
    print(json.dumps(report, sort_keys=True))
    return 0 if report["passed"] else 2


if __name__ == "__main__":
    raise SystemExit(main())

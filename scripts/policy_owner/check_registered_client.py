"""Exercise the real TEST CLI producer using only explicitly delivered imports.

No registered owner, authentication, provider, database or child is invoked.
The retained module/read pins identify the actual client dependency closure.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from hashlib import sha256
from pathlib import Path


def main():
    if not (sys.flags.isolated and sys.flags.no_site and sys.dont_write_bytecode):
        raise ValueError("client verification requires -I -B -S")
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--dependencies", required=True, type=Path)
    parser.add_argument(
        "--reviewed-client-image", action="append", nargs=2, default=[], metavar=("PATH", "SHA256")
    )
    args = parser.parse_args()
    source = Path(__file__).resolve().parents[2]
    dependencies = args.dependencies.resolve(strict=True)
    output = args.output.resolve(strict=False)
    if output.exists() or output.is_relative_to(source):
        raise ValueError("use a fresh external client-verification directory")
    output.mkdir()
    os.environ.clear()
    os.environ.update(
        SystemRoot=r"C:\Windows",
        SystemDrive="C:",
        WINDIR=r"C:\Windows",
        PATH=r"C:\Windows\System32",
        PATHEXT=".COM;.EXE;.BAT;.CMD",
        PYTHON_DOTENV_DISABLED="1",
        TRADINGAGENTS_OUTPUT_LANGUAGE="English",
        TRADINGAGENTS_CLI_ANNOUNCEMENTS_ENABLED="0",
        TUSHARE_TOKEN="fixture-only-production-binding",
        TRADINGAGENTS_PROJECT_ROOT=str(output / "unused-defaults"),
    )
    sys.path[:0] = [str(source), str(dependencies)]
    reads = set()

    def audit(event, values):
        if event.startswith("socket.") and event not in {"socket.__new__", "socket.gethostname"}:
            raise RuntimeError("client check forbids network/provider acquisition")
        if event in {"subprocess.Popen", "sqlite3.connect", "os.system", "os.startfile"}:
            raise RuntimeError("client check forbids subprocess/database/application activity")
        if event == "open" and not isinstance(values[0], int):
            value = os.fsdecode(values[0])
            if value.casefold() in {"nul", r"\\.\nul"}:
                return
            path = Path(value).resolve(strict=False)
            if values[2] & (os.O_WRONLY | os.O_RDWR | os.O_CREAT | os.O_TRUNC | os.O_APPEND):
                if not path.is_relative_to(output):
                    raise RuntimeError("client check write escaped its fresh output")
            else:
                reads.add(path)
        if event in {"os.mkdir", "os.remove", "os.rmdir", "os.chmod", "os.utime"} and not Path(
            values[0]
        ).resolve(strict=False).is_relative_to(output):
            raise RuntimeError("client check mutation escaped its fresh output")

    sys.addaudithook(audit)
    from scripts.policy_owner.registered_test_client import build_test_blueprint
    from scripts.policy_owner.run_registered_test import client_native_images

    registry = output / "SYNTHETIC-instrument-registry.json"
    registry.write_text(
        json.dumps(
            {
                "schema_version": "1.0",
                "registry_id": "synthetic-inert-client-registry-v1",
                "rows": [
                    {
                        "canonical_symbol": "601328.SS",
                        "aliases": [],
                        "venue": "XSHG",
                        "instrument_kind": "equity",
                        "currency": "CNY",
                        "display_name": "SYNTHETIC offline configuration fixture only",
                        "provenance": {
                            "provider": "SYNTHETIC offline fixture",
                            "source_ref": "fixture:synthetic-instrument-no-acquisition",
                            "retrieved_at": "2026-09-01T00:00:00Z",
                        },
                    }
                ],
            }
        ),
        encoding="utf-8",
    )
    blueprint, _ = build_test_blueprint(output / "SYNTHETIC-client", registry)
    reviewed_images = []
    for name, expected in args.reviewed_client_image:
        path = Path(name).resolve(strict=True)
        raw = path.read_bytes()
        if sha256(raw).hexdigest() != expected:
            raise ValueError("explicit client image pin mismatch")
        reviewed_images.append({"path": str(path), "byte_length": len(raw), "sha256": expected})
    native = client_native_images(dependencies, reviewed_images)
    base = Path(sys.base_prefix).resolve()
    modules = []
    entries = set()
    for name, module in tuple(sys.modules.items()):
        value = getattr(module, "__file__", None)
        if not value:
            continue
        path = Path(value).resolve()
        if not path.is_file():
            continue
        if not any(path.is_relative_to(root) for root in (source, dependencies, base)):
            raise ValueError("client module escaped delivered roots: " + str(path))
        raw = path.read_bytes()
        modules.append(
            {
                "module": name,
                "path": str(path),
                "byte_length": len(raw),
                "sha256": sha256(raw).hexdigest(),
            }
        )
    for path in tuple(reads):
        if path.is_relative_to(dependencies) and path.is_file():
            entries.add(path.relative_to(dependencies).parts[0])
    result = {
        "contract_version": "registered-test-client-check-v1",
        "passed": True,
        "interpreter": str(Path(sys.executable).resolve()),
        "source": str(source),
        "dependencies": str(dependencies),
        "module_search": sys.path,
        "modules": sorted(modules, key=lambda row: row["module"]),
        "dependency_top_level_entries": sorted(entries),
        "native_images": native,
        "blueprint": blueprint.model_dump(mode="json"),
        "product_modules_preloaded": False,
        "owner_contacted": False,
        "authentication_executed": False,
        "operational": False,
        "execution_authorized": False,
    }
    raw = json.dumps(result, sort_keys=True).encode("utf-8")
    (output / "RESULT.json").write_bytes(raw)
    print(
        json.dumps(
            {
                "passed": True,
                "output": str(output),
                "modules": len(modules),
                "dependency_entries": len(entries),
                "result_sha256": sha256(raw).hexdigest(),
            }
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

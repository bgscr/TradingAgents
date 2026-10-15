"""Two fresh processes: bounded acquisition, then network/credential-free reuse."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import subprocess
import sys
from pathlib import Path

SOURCE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SOURCE / "scripts"))
from diagnostic_runtime import install, safe_environment  # noqa: E402


def identities():
    names = [
        "scripts/accept_financial_statements.py",
        "scripts/diagnostic_runtime.py",
        "tradingagents/statement_acceptance.py",
        "tradingagents/tushare_financial_https.py",
        "tradingagents/dataflows/current_tushare_statements.py",
        "tradingagents/dataflows/current_statement_cache.py",
        "tradingagents/dataflows/financial_dispatch.py",
        "config/instrument_identity_registry.json",
        "config/instrument_identity_registry.sha256",
    ]
    return {name: hashlib.sha256((SOURCE / name).read_bytes()).hexdigest() for name in names}


def worker(args):
    root = args.output.resolve()
    contract = json.loads((root / "contract.json").read_text(encoding="utf-8"))
    if contract["sources"] != identities():
        raise RuntimeError("diagnostic source changed")
    if args.worker == "live" and not contract["live"]:
        raise RuntimeError("live worker not authorized by reservation")
    with (root / (args.worker + "-started.json")).open("x", encoding="utf-8") as handle:
        json.dump({"pid": os.getpid()}, handle)
    counts, blocked_network = install(root, SOURCE, offline=args.worker != "live")
    from tradingagents import statement_acceptance as acceptance

    acceptance.isolated_config(
        root, destination=contract["request"]["destination"], account=contract["request"]["account"]
    )
    from datetime import date

    date.fromisoformat(contract["request"]["as_of"])

    probes = {}
    import_counts = dict(counts)
    if args.worker == "reuse":
        import socket

        for name, probe in {
            "socket": lambda: socket.getaddrinfo("offline.invalid", 443),
            "environment": lambda: os.getenv("TUSHARE_TOKEN"),
            "file": lambda: (root / ".env").read_text(),
        }.items():
            try:
                probe()
            except RuntimeError:
                probes[name] = "blocked"
        baseline = dict(counts)
        result = acceptance.reuse(root, **contract["request"])
        result["access_during_application"] = {key: counts[key] - baseline[key] for key in counts}
    else:
        if args.worker == "synthetic":
            # Synthetic provider frames go through the real HTTPS JSON parser,
            # dispatcher, coordinator, normalizer and durable cache.
            from tests.test_current_tushare_statements import statement_rows
            from tradingagents import tushare_financial_https as transport

            class Connection:
                status = 200

                def __init__(self, host, **kwargs):
                    pass

                def request(self, method, path, *, body, headers):
                    self.endpoint = json.loads(body)["api_name"]

                def getresponse(self):
                    return self

                def read(self, limit):
                    frame = statement_rows(self.endpoint)
                    return json.dumps(
                        {
                            "code": 0,
                            "data": {"fields": list(frame.columns), "items": frame.values.tolist()},
                        }
                    ).encode()

                def close(self):
                    pass

            transport.http.client.HTTPSConnection = Connection
            token = "synthetic-only"
        else:
            import getpass

            if not sys.stdin.isatty():
                raise RuntimeError(
                    "manual live acceptance requires an interactive hidden token prompt"
                )
            token = getpass.getpass("Token for the explicitly selected destination (not saved): ")
        result = acceptance.acquire(root, **contract["request"], token=token)
        del token
    result.update(
        pid=os.getpid(),
        mode=args.worker,
        guard_counts=counts,
        guard_probes=probes,
        import_access=import_counts,
        blocked_network=blocked_network,
        sources=contract["sources"],
    )
    (root / (args.worker + ".json")).write_text(json.dumps(result, indent=2), encoding="utf-8")
    return 0 if result["status"] in {"acquired", "reused"} else 1


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument(
        "--destination", choices=["https://api.tushare.pro", "https://api.waditu.com/dataapi"]
    )
    parser.add_argument("--account")
    parser.add_argument("--as-of")
    parser.add_argument("--authorize-live", action="store_true")
    parser.add_argument("--worker", choices=["synthetic", "live", "reuse"], help=argparse.SUPPRESS)
    args = parser.parse_args()
    if not (sys.flags.isolated and sys.flags.no_site and sys.dont_write_bytecode):
        parser.error("use Python -I -S -B")
    if args.worker:
        return worker(args)
    if not all((args.destination, args.account, args.as_of)):
        parser.error("explicit destination, non-secret account label and as-of required")
    root = args.output.resolve()
    if root.is_relative_to(SOURCE) or root.exists():
        parser.error("fresh output directory outside source tree required")
    root.mkdir()  # Never resume an existing reservation or overwrite an old run.
    contract = {
        "sources": identities(),
        "request": {"destination": args.destination, "account": args.account, "as_of": args.as_of},
        "live": args.authorize_live,
        "physical_request_limit": 3,
        "redirects": 0,
        "retries": 0,
    }
    (root / "contract.json").write_text(json.dumps(contract, indent=2), encoding="utf-8")
    mode = "live" if args.authorize_live else "synthetic"
    for phase in (mode, "reuse"):
        command = [
            sys.executable,
            "-I",
            "-S",
            "-B",
            str(Path(__file__).resolve()),
            "--output",
            str(root),
            "--worker",
            phase,
        ]
        # Exactly two fixed sequential children; no shell, inherited credentials,
        # arbitrary command/worker path, or general subprocess permission.
        completed = subprocess.run(command, env=safe_environment(root), cwd=SOURCE, check=False)
        if completed.returncode:
            return completed.returncode
    first = json.loads((root / (mode + ".json")).read_text())
    second = json.loads((root / "reuse.json").read_text())
    passed = (
        first["pid"] != second["pid"]
        and first["physical_requests"] == 3
        and second["physical_requests"] == 0
        and second["client_attempts"] == 0
        and first["views"] == second["views"]
        and second["guard_probes"]
        == {"socket": "blocked", "environment": "blocked", "file": "blocked"}
        and second["import_access"]["credentials"] == 0
        and not any(second["access_during_application"].values())
    )
    summary = {
        "passed": passed,
        "acquisition_pid": first["pid"],
        "reuse_pid": second["pid"],
        "same_six_views_and_timestamps": first["views"] == second["views"],
        "live": args.authorize_live,
        "physical_requests": [first["physical_requests"], second["physical_requests"]],
    }
    (root / "result.json").write_text(json.dumps(summary, indent=2), encoding="utf-8")
    print(json.dumps(summary))
    return 0 if passed else 1


if __name__ == "__main__":
    raise SystemExit(main())

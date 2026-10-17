"""Two fresh processes: bounded acquisition, then network/credential-free reuse."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import subprocess
import sys
import unicodedata
import warnings
from pathlib import Path

SOURCE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SOURCE / "scripts"))
from diagnostic_runtime import install, safe_environment  # noqa: E402
from financial_network_guard import FinancialNetworkGuard  # noqa: E402

# Default-ignorable characters not all covered by Unicode's Cc/Cf/Cs categories:
# grapheme joiner, fillers, vowel/variation selectors, and reserved/tag ranges.
_INVISIBLE_INPUT_RANGES = (
    (0x034F, 0x034F), (0x115F, 0x1160), (0x17B4, 0x17B5), (0x180B, 0x180F),
    (0x3164, 0x3164), (0xFE00, 0xFE0F), (0xFFA0, 0xFFA0), (0xFFF0, 0xFFF8),
    (0xE0000, 0xE0FFF),
)


def identities():
    names = [
        "scripts/accept_financial_statements.py",
        "scripts/diagnostic_runtime.py",
        "scripts/financial_network_guard.py",
        "tradingagents/statement_acceptance.py",
        "tradingagents/tushare_financial_https.py",
        "tradingagents/dataflows/current_tushare_statements.py",
        "tradingagents/dataflows/current_statement_cache.py",
        "tradingagents/dataflows/financial_dispatch.py",
        "tradingagents/dataflows/_tushare_statement_normalization.py",
        "tradingagents/dataflows/financial_contracts.py",
        "tests/test_financial_acceptance_scope.py",
        "tests/test_current_tushare_statements.py",
        "config/instrument_identity_registry.json",
        "config/instrument_identity_registry.sha256",
    ]
    return {name: hashlib.sha256((SOURCE / name).read_bytes()).hexdigest() for name in names}


def _valid_manual_token(value):
    """Reject pasted wrappers without changing, describing or identifying a secret."""
    if not isinstance(value, str) or not value:
        return False
    if any(char.isspace() or unicodedata.category(char) in {"Cc", "Cf", "Cs"}
           or any(start <= ord(char) <= end for start, end in _INVISIBLE_INPUT_RANGES)
           for char in value):
        return False
    if value[0] in "\"'`“”‘’{[" or value[-1] in "\"'`“”‘’}]":
        return False
    return re.match(
        r"(?i)^(?:(?:\$env:)?(?:tushare[_-]?token|token|api[_-]?key|authorization|bearer)[:=]"
        r"|(?:[A-Za-z_]\w*=)?(?:[A-Za-z_]\w*\.)?(?:set_token|pro_api)\("
        r"|https?://)", value
    ) is None


def worker(args):
    root = args.output.resolve()
    # Exclusive reservation before any work. A duplicate worker may not replace
    # either an earlier phase receipt or its attempt marker.
    with (root / (args.worker + "-started.json")).open("x", encoding="utf-8") as handle:
        json.dump({"pid": os.getpid()}, handle)
    result = {"status": "stopped", "physical_requests": 0, "completed_tools": [], "views": {}}
    counts, blocked_network = {}, []
    stage = "reservation"
    try:
        contract = json.loads((root / "contract.json").read_text(encoding="utf-8"))
        if contract["sources"] != identities():
            raise RuntimeError("diagnostic source changed")
        if args.worker == "live" and not contract["live"]:
            raise RuntimeError("live worker not authorized by reservation")
        guard = FinancialNetworkGuard(contract["request"]["destination"])
        stage = "initialization"
        counts, blocked_network = install(
            root, SOURCE, offline=args.worker != "live", network_guard=guard
        )
        stage = "application"
        result.update(_worker(args, root, contract, counts, guard, result))
        result["sources"] = contract["sources"]
    except (Exception, KeyboardInterrupt) as exc:
        result["status"] = "stopped"
        result["failure"] = {
            "error_code": "acceptance_interrupted" if isinstance(exc, KeyboardInterrupt)
            else "acceptance_" + stage + "_failed",
            "retryable": False,
        }
    result.update(pid=os.getpid(), mode=args.worker, guard_counts=counts,
                  blocked_network=blocked_network)
    (root / (args.worker + ".json")).write_text(json.dumps(result, indent=2), encoding="utf-8")
    return 0 if result["status"] in {"acquired", "reused"} else 1


def _worker(args, root, contract, counts, guard, progress):
    from tradingagents import statement_acceptance as acceptance

    _, asset = acceptance.validate_request(root, **contract["request"])

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
        result = acceptance.reuse(root, **contract["request"], progress=progress)
        result["access_during_application"] = {key: counts[key] - baseline[key] for key in counts}
    else:
        if args.worker == "synthetic":
            # Synthetic provider frames go through the real HTTPS JSON parser,
            # dispatcher, coordinator, normalizer and durable cache.
            from tests.test_financial_acceptance_scope import sample_rows
            from tradingagents import tushare_financial_https as transport

            class Connection:
                status = 200

                def __init__(self, host, **kwargs):
                    self.host, self.port = host, 443

                def request(self, method, path, *, body, headers):
                    self.payload = json.loads(body)

                def getresponse(self):
                    return self

                def read(self, limit):
                    frame = sample_rows(self.payload["api_name"], self.payload["params"]["ts_code"])
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
            with warnings.catch_warnings():
                # getpass may otherwise fall back to visible input on some terminals.
                warnings.simplefilter("error", getpass.GetPassWarning)
                token = getpass.getpass(
                    "Tushare Token only (not a model API key; hidden, not saved): "
                )
        try:
            if not _valid_manual_token(token):
                return {"status": "stopped", "physical_requests": 0, "failure": {
                    "reason": "invalid_input", "error_code": "acceptance_token_input_invalid",
                    "provider_code": None, "classification_basis": "local_input",
                    "retryable": False,
                }}
            result = acceptance.acquire(root, **contract["request"], token=token,
                                        exchange_guard=guard.exchange, progress=progress)
        finally:
            del token
    result.update(
        guard_probes=probes,
        import_access=import_counts,
        canonical_symbol=asset.instrument_identity.symbol,
    )
    return result


def stopped(root, *, phase, code, receipts, live):
    failed = receipts.get(phase, {})
    summary = {
        "status": "stopped", "passed": False, "failed_phase": phase, "live": live,
        "failure": failed.get("failure", {"error_code": code, "retryable": False}),
        "physical_requests": [receipts.get(name, {}).get("physical_requests")
                              for name in ("live" if live else "synthetic", "reuse")],
        "phase_receipts": receipts,
    }
    (root / "result.json").write_text(json.dumps(summary, indent=2), encoding="utf-8")
    print(json.dumps(summary))
    return 1


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument(
        "--destination", choices=["https://api.tushare.pro", "https://api.waditu.com/dataapi"]
    )
    parser.add_argument("--account")
    parser.add_argument("--ticker")
    parser.add_argument("--as-of")
    parser.add_argument("--authorize-live", action="store_true")
    parser.add_argument("--worker", choices=["synthetic", "live", "reuse"], help=argparse.SUPPRESS)
    args = parser.parse_args()
    if not (sys.flags.isolated and sys.flags.no_site and sys.dont_write_bytecode):
        parser.error("use Python -I -S -B")
    if args.worker:
        return worker(args)
    if not all((args.destination, args.account, args.as_of, args.ticker)):
        parser.error("explicit ticker, destination, non-secret account label and as-of required")
    root = args.output.resolve()
    if root.is_relative_to(SOURCE) or root.exists():
        parser.error("fresh output directory outside source tree required")
    root.mkdir()  # Never resume an existing reservation or overwrite an old run.
    contract = {
        "sources": identities(),
        "request": {"destination": args.destination, "account": args.account,
                    "as_of": args.as_of, "ticker": args.ticker},
        "live": args.authorize_live,
        "physical_request_limit": 3,
        "redirects": 0,
        "retries": 0,
    }
    (root / "contract.json").write_text(json.dumps(contract, indent=2), encoding="utf-8")
    mode = "live" if args.authorize_live else "synthetic"
    receipts = {}
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
        try:
            completed = subprocess.run(command, env=safe_environment(root), cwd=SOURCE,
                                       check=False, timeout=180)
            receipt = root / (phase + ".json")
            if receipt.is_file():
                receipts[phase] = json.loads(receipt.read_text(encoding="utf-8"))
        except (Exception, KeyboardInterrupt):
            return stopped(root, phase=phase, code="acceptance_worker_incomplete",
                           receipts=receipts, live=args.authorize_live)
        if completed.returncode:
            return stopped(root, phase=phase, code="acceptance_worker_exit",
                           receipts=receipts, live=args.authorize_live)
        if phase not in receipts:
            return stopped(root, phase=phase, code="acceptance_missing_receipt",
                           receipts=receipts, live=args.authorize_live)
    first, second = receipts[mode], receipts["reuse"]
    passed = (
        first.get("status") == "acquired" and second.get("status") == "reused"
        and first.get("canonical_symbol") == second.get("canonical_symbol")
        and first.get("pid") != second.get("pid")
        and first.get("physical_requests") == 3
        and second.get("physical_requests") == 0
        and second.get("client_attempts") == 0
        and len(first.get("views", {})) == 6
        and first.get("views") == second.get("views")
        and second.get("guard_probes")
        == {"socket": "blocked", "environment": "blocked", "file": "blocked"}
        and second.get("import_access", {}).get("credentials") == 0
        and second.get("access_during_application") == {
            "network": 0, "credentials": 0, "subprocess": 0, "outside_write": 0
        }
    )
    if not passed:
        return stopped(root, phase="reuse", code="acceptance_comparison_failed",
                       receipts=receipts, live=args.authorize_live)
    summary = {
        "status": "passed" if passed else "stopped",
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

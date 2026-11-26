"""Two fresh processes: bounded acquisition, then network/credential-free reuse."""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import re
import subprocess
import sys
import time
import unicodedata
import warnings
from contextlib import contextmanager, nullcontext
from pathlib import Path

SOURCE = Path(__file__).resolve().parents[1]
PROJECT_ENV_DESTINATION = 'https://api.waditu.com/dataapi'
sys.path.insert(0, str(SOURCE / "scripts"))
from diagnostic_runtime import (  # noqa: E402
    AcceptanceInitializationProbe,
    SharedStatementStorage,
    install,
    safe_environment,
)
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
        "scripts/project_credentials.py",
        "scripts/session_token_pipe.py",
        "tradingagents/statement_acceptance.py",
        "tradingagents/dataflows/bank_identity_snapshot.py",
        "tradingagents/tushare_financial_https.py",
        "tradingagents/financial_response_completeness.py",
        "tradingagents/dataflows/current_tushare_statements.py",
        "tradingagents/dataflows/current_statement_cache.py",
        "tradingagents/dataflows/current_statement_diagnostics.py",
        "tradingagents/dataflows/financial_dispatch.py",
        "tradingagents/dataflows/_tushare_statement_normalization.py",
        "tradingagents/dataflows/financial_contracts.py",
        "tests/test_financial_acceptance_scope.py",
        "tests/test_current_tushare_statements.py",
        "config/instrument_identity_registry.json",
        "config/instrument_identity_registry.sha256",
    ]
    return {name: hashlib.sha256((SOURCE / name).read_bytes()).hexdigest() for name in names}


def _receipt_helpers():
    """Load only the pinned stdlib receipt module in the -I -S -B parent."""
    import importlib.util
    path = SOURCE / "tradingagents/dataflows/current_statement_diagnostics.py"
    spec = importlib.util.spec_from_file_location("financial_preparation_receipts", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


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
        binding = contract["request"].get("shared_binding")
        storage = SharedStatementStorage(binding["storage"]) if binding is not None else None
        grant = contract.get("initialization_policy")
        role = None
        if grant is not None:
            if (grant != _initialization_grant() or not contract["live"]
                    or storage is None or args.worker not in {"live", "reuse"}):
                raise RuntimeError("invalid_initialization_grant")
            role = args.worker
        stage = "initialization"
        credential_mode = contract.get('credential_mode', 'hidden')
        if credential_mode not in {'hidden', 'project_env', 'session_pipe'} or (
                credential_mode == 'project_env' and (
                    not contract['live'] or storage is None
                    or contract['request'].get('destination') != PROJECT_ENV_DESTINATION)):
            raise RuntimeError('invalid_credential_mode')
        if credential_mode == 'session_pipe':
            session_pipe_run_id(root, destination=contract['request']['destination'],
                                live=contract['live'], binding=binding)
            if role not in {'live', 'reuse'}:
                raise RuntimeError('session_token_initialization_required')
        installed = install(
            root, SOURCE, offline=args.worker != "live", network_guard=guard,
            shared_storage=storage, acceptance_initialization=role,
            project_env_credentials=args.worker == 'live' and credential_mode == 'project_env',
        )
        counts, blocked_network = installed[:2]
        initialization = installed[2] if role is not None else None
        stage = "application"
        result.update(_worker(args, root, contract, counts, guard, result, storage, initialization))
        result["sources"] = contract["sources"]
    except (Exception, KeyboardInterrupt) as exc:
        from project_credentials import credential_failure_code
        result["status"] = "stopped"
        result["failure"] = {
            "error_code": "acceptance_interrupted" if isinstance(exc, KeyboardInterrupt)
            else "acceptance_" + stage + "_failed",
            "retryable": False,
            "credential_failure": credential_failure_code(exc),
        }
    result.update(pid=os.getpid(), mode=args.worker, guard_counts=counts,
                  blocked_network=blocked_network)
    (root / (args.worker + ".json")).write_text(json.dumps(result, indent=2), encoding="utf-8")
    return 0 if result["status"] in {"acquired", "reused", "acquired_degraded", "reused_degraded"} else 1


def _worker(args, root, contract, counts, guard, progress, storage=None, initialization=None):
    from tradingagents import statement_acceptance as acceptance

    request = {**contract["request"], "shared_storage": storage}
    _, asset = acceptance.validate_request(root, **request)
    optional_gaps = contract.get("allow_optional_financial_gaps", False)
    if type(optional_gaps) is not bool:
        raise ValueError("invalid optional financial preparation mode")

    if initialization is not None:
        initialization.finish_initialization()
    probes = {}
    import_counts = dict(counts)
    if args.worker == "reuse":
        import socket

        for name, probe in {
            "socket": lambda: socket.getaddrinfo("offline.invalid", 443),
            "environment": lambda: os.getenv("TUSHARE_TOKEN"),
            "file": lambda: (root / ".env").read_text(),
        }.items():
            expected = initialization.expected_denial(name) if initialization else nullcontext()
            with expected:
                try:
                    probe()
                except RuntimeError:
                    probes[name] = "blocked"
        if initialization is not None:
            initialization.begin_application()
        baseline = dict(counts)
        result = acceptance.reuse(root, **request, progress=progress, allow_optional_gaps=optional_gaps)
        result["access_during_application"] = {key: counts[key] - baseline[key] for key in counts}
    else:
        full_client=None
        if "full_params_sha256" in contract or (root.parent/"full-owner.json").exists():
            from full_financial_channel import Client
            if args.worker!="live":raise ValueError("full_live_phase_required")
            full_client=Client(SOURCE,root,contract,sys.stdin.buffer)
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
                            "data": {"fields": list(frame.columns), "items": frame.values.tolist(),
                                     "has_more": False, "total": len(frame)},
                        }
                    ).encode()

                def close(self):
                    pass

            transport.http.client.HTTPSConnection = Connection
            token = "synthetic-only"
        elif contract.get('credential_mode', 'hidden') == 'session_pipe':
            from session_token_pipe import read_session_token
            if _worker_timeout(contract.get('session_deadline_epoch'),
                               contract.get('session_monotonic_deadline')) <= 0:
                raise RuntimeError('session_token_deadline_exceeded')
            token = read_session_token(sys.stdin.buffer, root.parent.name)
        elif contract.get('credential_mode', 'hidden') == 'project_env':
            from project_credentials import read_project_credentials
            token = read_project_credentials(SOURCE, names=('TUSHARE_TOKEN',))['TUSHARE_TOKEN']
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
            if initialization is not None:
                initialization.begin_application()
            def exchange(connection, endpoint):
                authority = (initialization.exchange(guard, connection, endpoint)
                             if initialization is not None else guard.exchange(connection, endpoint))
                if full_client is None:return bounded_exchange(authority, connection, contract)
                from contextlib import contextmanager
                @contextmanager
                def reserved():
                    with bounded_exchange(authority,connection,contract):
                        remaining=full_client.exchange(endpoint)
                        connection.timeout=min(connection.timeout,remaining)
                        yield
                return reserved()

            result = acceptance.acquire(root, **request, token=token, allow_optional_gaps=optional_gaps,
                                        exchange_guard=exchange,
                                         progress=progress)
        finally:
            del token
    if initialization is not None:
        initialization.finish_application()
        result["initialization_policy"] = initialization.summary()
    result.update(
        guard_probes=probes,
        import_access=import_counts,
        canonical_symbol=asset.instrument_identity.symbol,
    )
    return result


def stopped(root, *, phase, code, receipts, live, worker_returncode=None):
    failed = receipts.get(phase, {})
    summary = {
        "status": "stopped", "passed": False, "failed_phase": phase, "live": live,
        "failure": failed.get("failure", {"error_code": code, "retryable": False}),
        "physical_requests": [receipts.get(name, {}).get("physical_requests")
                              for name in ("live" if live else "synthetic", "reuse")],
        "phase_receipts": receipts,
        # A hard guard exit intentionally bypasses the worker's normal receipt.
        # Preserve its exit status without capturing hidden-input streams or
        # incorrectly replacing an unknown request count with zero.
        "worker_returncode": worker_returncode if (
            type(worker_returncode) is int and -(2 ** 31) <= worker_returncode < 2 ** 32
        ) else None,
    }
    if phase in {"live", "reuse", "synthetic"}:
        initialization = root / (phase + "-initialization.json")
        if initialization.is_file() and not initialization.is_symlink():
            summary["initialization_receipt"] = initialization.name
    (root / "result.json").write_text(json.dumps(summary, indent=2), encoding="utf-8")
    print(json.dumps(summary))
    return 1


def load_shared_binding(path, *, destination, account, ticker, as_of):
    """Read only an explicitly supplied, bounded, non-secret binding document."""
    path = SharedStatementStorage.path(path)
    if (path.suffix != ".json" or path.name == "tradingagents.local.json"
            or path.name.startswith(".env") or path.stat().st_size > 16384):
        raise ValueError("bounded non-secret shared binding JSON required")

    def unique(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                raise ValueError("duplicate binding key")
            result[key] = value
        return result

    binding = json.loads(path.read_text(encoding="utf-8"), object_pairs_hook=unique)
    keys = {"contract", "account", "destination", "instrument_identity_revision", "as_of",
            "announcement_start", "announcement_end", "normalizer_version", "storage"}
    if (not isinstance(binding, dict) or set(binding) not in (keys, keys | {'bank_identity'}, keys | {'equity_identity'})
            or binding["contract"] != "shared-statement-acceptance-v1"
            or binding["account"] != account or binding["destination"] != destination
            or binding["as_of"] != as_of
            or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}", account or "")
            or not re.fullmatch(r"[0-9]{6}\.(SH|SS|SZ)", ticker or "")):
        raise ValueError("shared binding request mismatch")
    for key in ("as_of", "announcement_start", "announcement_end"):
        if not isinstance(binding[key], str) or not re.fullmatch(r"\d{4}-\d{2}-\d{2}", binding[key]):
            raise ValueError("invalid binding date")
    for key in ("instrument_identity_revision", "normalizer_version"):
        if not isinstance(binding[key], str) or not re.fullmatch(
            r"[A-Za-z0-9][A-Za-z0-9_.:=-]{0,255}", binding[key]
        ):
            raise ValueError("invalid binding identity")
    SharedStatementStorage(binding["storage"])
    return binding



def _initialization_grant():
    return {"contract": AcceptanceInitializationProbe.POLICY,
            "maximum_per_worker": 1, "maximum_total": 2}


def _initialization_verified(receipt, role, *, project_env=False):
    if type(project_env) is not bool or (project_env and role != 'live'):
        return False
    value = receipt.get("initialization_policy")
    if not isinstance(value, dict):
        return False
    expected = {"socket": 1, "environment": 1, "file": 1} if role == "reuse" else {
        "socket": 0, "environment": 0, "file": 0
    }
    counts = value.get("counts")
    zero = {"network": 0, "credentials": 0, "subprocess": 0, "outside_write": 0}
    expected_import = {**zero, 'project_env_reads': 0} if project_env else zero
    expected_final = {**zero, 'project_env_reads': 1} if project_env else zero
    return (
        value.get("contract") == AcceptanceInitializationProbe.POLICY
        and value.get("role") == role and value.get("phase") == "complete"
        and value.get("implementation_sha256") == AcceptanceInitializationProbe.SOURCE_SHA256
        and value.get("version_sha256") == AcceptanceInitializationProbe.VERSION_SHA256
        and value.get("probe_finished") is True
        and type(value.get("unexpected_denials")) is int and value["unexpected_denials"] == 0
        and value.get("expected_denials") == expected
        and all(type(n) is int for n in value["expected_denials"].values())
        and value.get("first_unexpected_denial") is None
        and type(counts) is dict
        and set(counts) == {"function_calls", "socket_creations", "allowed_bind_attempts", "closed_sockets"}
        and all(type(n) is int for n in counts.values())
        and counts["function_calls"] == 1
        and 0 <= counts["allowed_bind_attempts"] <= counts["socket_creations"] <= 1
        and counts["closed_sockets"] == counts["allowed_bind_attempts"]
        and receipt.get("import_access") == expected_import
        and all(type(n) is int for n in receipt['import_access'].values())
        and (role != "live" or (receipt.get("guard_counts") == expected_final
                                and all(type(n) is int for n in receipt['guard_counts'].values())))
    )


@contextmanager
def bounded_exchange(authority, connection, contract):
    with authority:
        remaining = _worker_timeout(contract.get('session_deadline_epoch'),
                                    contract.get('session_monotonic_deadline'))
        if remaining <= 0:
            raise RuntimeError('financial_send_deadline_exceeded')
        connection.timeout = min(30, remaining)
        yield


def _worker_timeout(deadline, monotonic_deadline, *, now=None, monotonic_now=None):
    """The established 180s worker cap may shrink, never expand."""
    if deadline is None and monotonic_deadline is None:
        return 180
    if any(type(value) not in {int, float} or not math.isfinite(value) or value <= 0
           for value in (deadline, monotonic_deadline)):
        raise ValueError('invalid session deadline')
    now = time.time() if now is None else now
    monotonic_now = time.monotonic() if monotonic_now is None else monotonic_now
    return max(0.0, min(180, deadline - now, monotonic_deadline - monotonic_now))


def output_path_allowed(root, source=SOURCE, *, require_fresh=True):
    """Keep diagnostic outputs external; allow only the ordinary entry's owned child."""
    root, source = Path(root).absolute(), Path(source).absolute()
    if '..' in root.parts or '..' in source.parts:
        return False
    for path in dict.fromkeys((root, *root.parents, source, *source.parents)):
        if path.exists() and (path.is_symlink()
                or getattr(path.lstat(), 'st_file_attributes', 0) & 0x400):
            return False
    # Windows resolves trailing dots/spaces and short names to the existing directory.
    # Check reparse points first, then classify by the actual existing ancestor identity.
    root, source = root.resolve(), source.resolve()
    if require_fresh and root.exists():
        return False
    if not root.is_relative_to(source):
        return True
    parts = root.relative_to(source).parts
    if (len(parts) != 4 or parts[:2] != ('reports', 'bounded-runs')
            or re.fullmatch(r'[0-9a-f]{32}', parts[2]) is None or parts[3] != 'refresh'):
        return False
    record = root.parent / 'session.json'
    if (not record.is_file() or record.is_symlink() or record.stat().st_nlink != 1
            or getattr(record.lstat(), 'st_file_attributes', 0) & 0x400
            or record.stat().st_size > 2048):
        return False
    try:
        value = json.loads(record.read_text(encoding='utf-8'))
    except (ValueError, OSError):
        return False
    return value == {'contract': 'bounded-mainland-session-v1', 'run_id': root.parent.name,
                     'root': str(root.parent), 'resume': False}


def session_pipe_run_id(root, *, destination, live, binding):
    """Only an existing, owned bounded session can receive a one-use token."""
    if (live is not True or binding is None or destination != PROJECT_ENV_DESTINATION
            or not root.resolve().is_relative_to(SOURCE / 'reports/bounded-runs')
            or not output_path_allowed(root, source=SOURCE, require_fresh=False)):
        raise ValueError('session_token_context_invalid')
    return root.parent.name


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument(
        "--destination", choices=["https://api.tushare.pro", "https://api.waditu.com/dataapi"]
    )
    parser.add_argument("--account")
    parser.add_argument("--ticker")
    parser.add_argument("--as-of")
    parser.add_argument("--shared-binding", type=Path,
                        help="Explicit non-secret shared storage and request binding JSON")
    parser.add_argument("--authorize-live", action="store_true")
    parser.add_argument("--allow-optional-financial-gaps", action="store_true",
                        help="Verify noncritical financial gaps without promoting them to financial facts.")
    parser.add_argument('--use-project-env', action='store_true',
                        help='Explicit fixed-project TUSHARE_TOKEN input for a bounded live session only')
    parser.add_argument("--authorize-initialization-probes", action="store_true",
                        help="Explicit live/reuse initialization policy: at most one pinned probe per worker")
    parser.add_argument("--session-token-stdin", action="store_true", help=argparse.SUPPRESS)
    parser.add_argument("--full-ticket-stdin",action="store_true",help=argparse.SUPPRESS)
    parser.add_argument("--worker", choices=["synthetic", "live", "reuse"], help=argparse.SUPPRESS)
    parser.add_argument("--session-deadline-epoch", type=float,
                        help="Optional shared session deadline; never extends the 180s worker cap")
    parser.add_argument("--session-monotonic-deadline", type=float,
                        help="Original same-machine monotonic deadline, paired with the epoch deadline")
    args = parser.parse_args()
    deadlines = (args.session_deadline_epoch, args.session_monotonic_deadline)
    if any(value is not None for value in deadlines) and any(
            value is None or not math.isfinite(value) or value <= 0 for value in deadlines):
        parser.error('invalid session deadline')
    if not (sys.flags.isolated and sys.flags.no_site and sys.dont_write_bytecode):
        parser.error("use Python -I -S -B")
    if args.worker:
        return worker(args)
    if not all((args.destination, args.account, args.as_of, args.ticker)):
        parser.error("explicit ticker, destination, non-secret account label and as-of required")
    if args.authorize_initialization_probes and not (args.authorize_live and args.shared_binding):
        parser.error("initialization probes require explicit live shared acceptance")
    if args.allow_optional_financial_gaps and args.shared_binding is None:
        parser.error("optional gaps require an explicit shared binding")
    root = args.output.absolute()
    if args.use_project_env and (not args.authorize_live or not args.shared_binding
                                or args.destination != PROJECT_ENV_DESTINATION
                                or not root.is_relative_to(SOURCE / 'reports/bounded-runs')):
        parser.error('project env mode requires an owned bounded live session')
    if not output_path_allowed(root):
        parser.error("fresh external output or exact owned bounded-session refresh path required")
    binding = None
    if args.shared_binding is not None:
        if not args.authorize_live:
            parser.error("shared storage is not a synthetic-output target")
        binding = load_shared_binding(args.shared_binding, destination=args.destination,
                                      account=args.account, ticker=args.ticker, as_of=args.as_of)
    if args.session_token_stdin:
        if (args.use_project_env or not args.authorize_initialization_probes
                or args.session_deadline_epoch is None):
            parser.error('session token pipe requires exclusive bounded initialization mode')
        session_pipe_run_id(root, destination=args.destination, live=args.authorize_live,
                            binding=binding)
        from session_token_pipe import require_pipe
        require_pipe(sys.stdin.buffer)  # Validate the handle without reading any bytes.
    full_spec=None
    equity_request = isinstance(binding, dict) and "equity_identity" in binding
    if args.full_ticket_stdin or equity_request or (root.parent/"full-owner.json").exists():
        if not (args.full_ticket_stdin and binding and args.authorize_live and args.authorize_initialization_probes):
            parser.error("Full financial owner ticket required")
        from full_financial_channel import binding as full_binding, effective_day
        from session_token_pipe import require_pipe
        require_pipe(sys.stdin.buffer)
        full_spec=full_binding(SOURCE,root.parent)
        if equity_request and full_spec["params"].get("contract") != "default-full-latest-intake-v1":
            parser.error("Equity overlay requires the confirmed latest Full owner")
        if (full_spec["params"]["ticker"]!=args.ticker.replace(".SS",".SH") or
            effective_day(full_spec,root.parent)!=args.as_of or full_spec["account"]!=args.account):
            parser.error("Full request binding mismatch")
    root.mkdir()  # Never resume an existing reservation or overwrite an old run.
    contract = {
        "sources": identities(),
        "request": {"destination": args.destination, "account": args.account,
                    "as_of": args.as_of, "ticker": args.ticker},
        "live": args.authorize_live,
        "physical_request_limit": 3,
        "allow_optional_financial_gaps": args.allow_optional_financial_gaps,
        "redirects": 0,
        "retries": 0,
        "credential_mode": ('session_pipe' if args.session_token_stdin else
                            'project_env' if args.use_project_env else 'hidden'),
    }
    if full_spec is not None:contract["full_params_sha256"]=full_spec["params_sha256"]
    if args.session_deadline_epoch is not None:
        contract['session_deadline_epoch'] = args.session_deadline_epoch
        contract['session_monotonic_deadline'] = args.session_monotonic_deadline
    if binding is not None:
        contract["request"]["shared_binding"] = binding
    if args.authorize_initialization_probes:
        contract["initialization_policy"] = _initialization_grant()
    (root / "contract.json").write_text(json.dumps(contract, indent=2), encoding="utf-8")
    mode = "live" if args.authorize_live else "synthetic"
    receipts = {}
    for phase in (mode, "reuse"):
        remaining = _worker_timeout(args.session_deadline_epoch, args.session_monotonic_deadline)
        if remaining <= 0:
            return stopped(root, phase=phase, code='acceptance_session_deadline_exceeded',
                           receipts=receipts, live=args.authorize_live)
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
            if args.session_token_stdin or args.full_ticket_stdin:
                from session_token_pipe import run_worker
                worker_input = sys.stdin.buffer if phase == 'live' else subprocess.DEVNULL
                try:
                    completed = run_worker(command, env=safe_environment(root), cwd=SOURCE,
                                           stdin=worker_input, timeout=remaining)
                finally:
                    if phase == 'live':
                        sys.stdin.close()  # Never let reuse inherit the one-use pipe.
            else:
                # Preserve the standalone hidden/project-env/synthetic invocation
                # contract and existing test seam. Pipe supervision is opt-in.
                completed = subprocess.run(command, env=safe_environment(root), cwd=SOURCE,
                                           check=False, timeout=remaining)
            receipt = root / (phase + ".json")
            if receipt.is_file():
                receipts[phase] = json.loads(receipt.read_text(encoding="utf-8"))
        except (Exception, KeyboardInterrupt):
            return stopped(root, phase=phase, code="acceptance_worker_incomplete",
                           receipts=receipts, live=args.authorize_live)
        if completed.returncode:
            return stopped(root, phase=phase, code="acceptance_worker_exit",
                           receipts=receipts, live=args.authorize_live,
                           worker_returncode=completed.returncode)
        if phase not in receipts:
            return stopped(root, phase=phase, code="acceptance_missing_receipt",
                           receipts=receipts, live=args.authorize_live)
    first, second = receipts[mode], receipts["reuse"]
    passed = (
        first.get("status") in ({"acquired", "acquired_degraded"} if args.allow_optional_financial_gaps else {"acquired"})
        and second.get("status") == ("reused_degraded" if first.get("status") == "acquired_degraded" else "reused")
        and first.get("canonical_symbol") == second.get("canonical_symbol")
        and first.get("pid") != second.get("pid")
        and type(first.get("physical_requests")) is int
        and 0 <= first["physical_requests"] <= 3
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
    if args.authorize_initialization_probes:
        passed = (passed and _initialization_verified(first, 'live', project_env=args.use_project_env)
                  and _initialization_verified(second, 'reuse'))
    try:
        receipt_helpers = _receipt_helpers()
        expected_symbol = ((args.ticker[:-3] + ".SS" if args.ticker.endswith(".SH") else args.ticker)
                           if binding else first.get("canonical_symbol"))
        if first.get("canonical_symbol") != expected_symbol:
            raise ValueError("financial_preparation_symbol_mismatch")
        gap_count = receipt_helpers.validate_prepared_views(first.get("views", {}), ticker=expected_symbol,
                                           as_of=args.as_of, allow_optional_gaps=args.allow_optional_financial_gaps,
                                           expected_scopes=receipt_helpers.scopes_from_binding(binding) if binding else None)
        passed = passed and ((gap_count > 0) == (first.get("status") == "acquired_degraded"))
    except (TypeError, ValueError):
        passed = False
        gap_count = 0
    if not passed:
        return stopped(root, phase="reuse", code="acceptance_comparison_failed",
                       receipts=receipts, live=args.authorize_live)
    summary = {
        "status": "passed_degraded" if gap_count else "passed",
        "degraded": bool(gap_count),
        "passed": passed,
        "acquisition_pid": first["pid"],
        "reuse_pid": second["pid"],
        "same_six_views_and_timestamps": not gap_count and first["views"] == second["views"],
        "same_six_outcomes_and_timestamps": first["views"] == second["views"],
        "live": args.authorize_live,
        "physical_requests": [first["physical_requests"], second["physical_requests"]],
    }
    (root / "result.json").write_text(json.dumps(summary, indent=2), encoding="utf-8")
    print(json.dumps(summary))
    return 0 if passed else 1


if __name__ == "__main__":
    raise SystemExit(main())


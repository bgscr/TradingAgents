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


class SharedStatementStorage:
    """Exact manual-run storage binding; never a directory-wide write grant.

    Only use in the single-threaded acceptance worker. The declared file ID is
    retained across workers; an account label cannot identify a counter database.
    """

    OWNER = ".market-history-database-owner"

    def __init__(self, binding):
        import re

        if not isinstance(binding, dict) or set(binding) != {
            "market_database", "coordinator_database", "cache_database", "coordinator_file_id"
        }:
            raise ValueError("invalid shared storage binding")
        self.market = self.path(binding["market_database"])
        self.coordinator = self.path(binding["coordinator_database"])
        self.cache = self.path(binding["cache_database"])
        if self.market.suffix != ".sqlite3":
            raise ValueError("explicit market SQLite file required")
        self.file_id = binding["coordinator_file_id"]
        self._connect_thread = None
        if (not isinstance(self.file_id, list) or len(self.file_id) != 2
                or any(type(n) is not int or n < 0 for n in self.file_id)
                or self.file_id[1] == 0):
            raise ValueError("explicit coordinator file identity required")
        if self.coordinator != self.market.with_name(
            self.market.name + ".provider-requests.sqlite3"
        ) or self.cache.name != "current-financial-statements.sqlite3":
            raise ValueError("shared storage path identity mismatch")
        if len({self.market, self.coordinator, self.cache}) != 3:
            raise ValueError("shared stores must be distinct")
        self.owner_dir = self.coordinator.with_name(self.coordinator.name + ".payloads")
        self.owner = self.owner_dir / self.OWNER
        self.owner_pattern = re.compile(re.escape(self.OWNER) + r"\.[0-9a-f]{32}\.tmp")
        self.sidecars = {
            Path(str(db) + suffix)
            for db in (self.coordinator, self.cache)
            for suffix in ("-wal", "-shm", "-journal")
        }
        self.directories = {self.coordinator.parent, self.cache.parent, self.owner_dir}
        self.check_existing()

    @staticmethod
    def path(value):
        if not isinstance(value, (str, os.PathLike)):
            raise ValueError("explicit filesystem path required")
        path = Path(value)
        if not path.is_absolute() or ".." in path.parts or str(value).startswith("file:"):
            raise ValueError("absolute non-traversing path required")
        if any(part.casefold() in {".ssh", ".aws", ".azure", "tradingagents.local.json"}
               or part.casefold().startswith(".env") for part in path.parts):
            raise ValueError("protected storage path prohibited")
        for item in (path, *path.parents):
            if item.is_symlink() or item.is_junction():
                raise ValueError("linked storage paths prohibited")
        if path.exists() and path.is_file() and path.stat().st_nlink != 1:
            raise ValueError("hard-linked storage file prohibited")
        return path.resolve()

    def check_existing(self):
        for item in (self.market, self.coordinator, self.owner, self.cache, *self.sidecars):
            self.path(item)
        if not all(path.is_dir() for path in self.directories):
            raise ValueError("shared storage directories must already exist")
        if not all(path.is_file() for path in (self.market, self.coordinator, self.owner)):
            raise ValueError("existing shared authority required")
        stat = self.coordinator.stat()
        if [stat.st_dev, stat.st_ino] != self.file_id or stat.st_size == 0:
            raise ValueError("shared coordinator replaced or empty")
        if self.owner.stat().st_size > 4096 or self.owner.read_bytes() != (
            os.path.normcase(str(self.coordinator)).encode("utf-8") + b"\n"
        ):
            raise ValueError("shared coordinator ownership mismatch")

    def is_owner_temp(self, path):
        return path.parent == self.owner_dir and self.owner_pattern.fullmatch(path.name) is not None

    def permits(self, path, operation):
        path = self.path(path)
        if operation == "read":
            return path in {self.coordinator, self.cache, self.owner, *self.sidecars}
        if operation == "mkdir":
            return path in self.directories and path.is_dir()
        if operation == "remove":
            return path in self.sidecars or self.is_owner_temp(path)
        if operation == "write":
            return path in {self.coordinator, self.cache, *self.sidecars} or self.is_owner_temp(path)
        return False

    def permits_link(self, source, target):
        source, target = self.path(source), self.path(target)
        return self.is_owner_temp(source) and target == self.owner and target.is_file()

    def database_path(self, value):
        # Only our two fixed URI forms, not arbitrary SQLite URI options.
        if str(value) in {self.coordinator.as_uri() + "?mode=ro",
                          self.coordinator.as_uri() + "?mode=rw"}:
            return self.coordinator
        return self.path(value)

    def validate_authority(self, schema_version, upstream_id, service):
        import sqlite3
        from contextlib import closing

        self.check_existing()
        with closing(sqlite3.connect(self.coordinator.as_uri() + "?mode=ro", uri=True)) as db:
            version = db.execute("SELECT MAX(version) FROM schema_migrations").fetchone()[0]
            imported = db.execute(
                "SELECT 1 FROM provider_request_state_imports WHERE source_database_path=?",
                (os.path.normcase(str(self.market)),),
            ).fetchone()
            identity = db.execute(
                "SELECT service_name, account_scope, operator_ceiling_json "
                "FROM upstream_services WHERE upstream_service_id=?",
                (upstream_id,),
            ).fetchone()
        if (version != schema_version or imported is None or identity is None
                or identity[:2] != (service, "") or identity[2] is None):
            raise ValueError("existing shared authority schema/import/service mismatch")
        return identity[2]

    def check_connection_event(self, database):
        """Audit catches saved aliases/direct constructors before opening storage."""
        import threading

        path = self.database_path(database)
        if path in {self.coordinator, self.cache}:
            if self._connect_thread != threading.get_ident():
                raise DeniedAccess("shared SQLite connection must use bound factory")
            self.check_existing()
        return path

    def connection_factory(self, original):
        """Protect both shared databases against native ATTACH/VACUUM writes."""
        import sqlite3
        import threading
        if getattr(original, "_statement_storage", None) is self:
            return original

        def connect(database, *args, **kwargs):
            path = self.database_path(database)
            if path not in {self.coordinator, self.cache}:
                return original(database, *args, **kwargs)
            self.check_existing()
            if args or "factory" in kwargs or self._connect_thread is not None:
                raise ValueError("unexpected shared SQLite connection arguments or concurrency")
            if path == self.coordinator:
                readonly = str(database) == self.coordinator.as_uri() + "?mode=ro"
                kwargs = {**kwargs, "uri": True}
                database = self.coordinator.as_uri() + ("?mode=ro" if readonly else "?mode=rw")
            self._connect_thread = threading.get_ident()
            try:
                connection = original(database, **kwargs)
            finally:
                self._connect_thread = None
            try:
                self.check_existing()
                connection.set_authorizer(
                    lambda action, *unused: sqlite3.SQLITE_DENY
                    if action in {sqlite3.SQLITE_ATTACH, sqlite3.SQLITE_DETACH}
                    else sqlite3.SQLITE_OK
                )
            except BaseException:
                connection.close()
                raise
            return connection

        connect._statement_storage = self
        return connect

    def connection_scope(self):
        """Single-threaded worker: keep real scheduling/counting; copy no state."""
        import _sqlite3
        import sqlite3
        from contextlib import contextmanager
        from unittest.mock import patch

        @contextmanager
        def scope():
            factory = self.connection_factory(sqlite3.connect)
            with (patch.object(sqlite3, "connect", factory),
                  patch.object(sqlite3.dbapi2, "connect", factory),
                  patch.object(_sqlite3, "connect", factory)):
                yield

        return scope()


class Urllib3LoopbackProbe:
    """Explicit test-only exception for one pinned, trusted CPython import path.

    Not an OS sandbox or protection against hostile same-process reflection.
    Never instantiated by the default diagnostic or real acceptance CLI.
    """

    POLICY = "urllib3-2.7.0-ipv6-import-once-v1"
    SOURCE_SHA256 = "2633bbdb69731e5ccb5cf4e4afd65605d86c7979cc5633126f50c92d5ad74a74"
    VERSION_SHA256 = "fa032b2a1b0f88e8f45f351ecd515ae7d7754ba4204222a0413e601a0c8cf3ec"

    def __init__(self, root, dependencies, *, role="cold_child"):
        import _socket
        import hashlib
        import json
        import marshal
        import socket
        import ssl
        import threading
        import types

        if role not in {"parent", "cold_child"}:
            raise ValueError("invalid_probe_role")
        self.role = role
        if sys.implementation.name != "cpython" or sys.version_info[:3] != (3, 13, 7):
            raise RuntimeError("probe_policy_interpreter_mismatch")
        if sys.flags.optimize or sys.getprofile() is not None:
            raise RuntimeError("probe_policy_observer_conflict")
        self.path = Path(dependencies).resolve() / "urllib3" / "util" / "connection.py"
        version_path = self.path.parent.parent / "_version.py"
        for path in (self.path, version_path):
            if any(p.is_symlink() or p.is_junction() for p in (path, *path.parents)):
                raise RuntimeError("probe_policy_linked_implementation")
        raw = self.path.read_bytes()
        if (hashlib.sha256(raw).hexdigest() != self.SOURCE_SHA256
                or hashlib.sha256(version_path.read_bytes()).hexdigest() != self.VERSION_SHA256):
            raise RuntimeError("probe_policy_implementation_mismatch")
        compiled = compile(raw, str(self.path), "exec", dont_inherit=True, optimize=0)

        def fingerprint(code):
            constants = tuple(normalize(v) if isinstance(v, types.CodeType) else v
                              for v in code.co_consts)
            return code.replace(co_filename="<pinned-source>", co_consts=constants)

        normalize = fingerprint
        self.code_digest = lambda code: hashlib.sha256(marshal.dumps(normalize(code))).digest()
        functions = [c for c in compiled.co_consts
                     if isinstance(c, types.CodeType) and c.co_name == "_has_ipv6"]
        if len(functions) != 1:
            raise RuntimeError("probe_policy_function_mismatch")
        self.function_digest = self.code_digest(functions[0])
        self.module_digest = self.code_digest(compiled)
        self.socket_module = socket
        self.ssl_socket_type = ssl.SSLSocket
        self.socket_type = socket.socket
        self.constructor_code = socket.socket.__init__.__code__
        self.close_method = socket.socket.close
        self.native_fileno = _socket.socket.fileno
        self.thread_id = threading.get_ident()
        self.get_ident = threading.get_ident
        self.root = Path(root).resolve()
        self.receipt = self.root / "loopback-probe-policy.json"
        if self.receipt.exists():
            raise RuntimeError("probe_policy_receipt_exists")
        self.json = json
        self.frame = None
        self.sock = None
        self.used = False
        self.finished = False
        self.counts = {"function_calls": 0, "socket_creations": 0,
                       "allowed_bind_attempts": 0, "closed_sockets": 0}
        self.callback = self._profile

    def start(self):
        if self.used or sys.getprofile() is not None:
            raise RuntimeError("probe_policy_cannot_restart")
        self.seal_socket_methods()
        sys.setprofile(self.callback)

    def seal_socket_methods(self):
        def no_traffic(*args, **kwargs):
            self._stop("forbidden_socket_method")
        # These operations do not all emit CPython audit events. Preserve the
        # constructor and close; do not fabricate any successful network result.
        for owner in (self.socket_type, self.ssl_socket_type):
            for name in ("connect", "connect_ex", "listen", "accept", "send",
                         "sendall", "sendto", "sendmsg", "write"):
                if hasattr(owner, name):
                    setattr(owner, name, no_traffic)

    def _save(self, status, reason):
        data = {"contract": self.POLICY, "version": "2.7.0", "role": self.role, "pid": os.getpid(),
                "implementation_sha256": self.SOURCE_SHA256, "status": status, "reason": reason, "counts": dict(self.counts)}
        if status == "stopped":
            data.update(_probe_denial_metadata(reason))
        self.receipt.write_text(self.json.dumps(data, sort_keys=True), encoding="utf-8")

    def _stop(self, reason):
        # A profile exception would unregister the callback and could be swallowed
        # by urllib3. Never resume after a policy/lifecycle/observer failure.
        try:
            self._save("stopped", reason)
        finally:
            os._exit(74)

    def _enter(self, frame):
        caller = frame.f_back
        module = sys.modules.get("urllib3.util.connection")
        if (self.used or self.get_ident() != self.thread_id or caller is None
                or self.code_digest(frame.f_code) != self.function_digest
                or self.code_digest(caller.f_code) != self.module_digest
                or caller.f_lineno != 137 or caller.f_globals is not frame.f_globals
                or module is None or module.__dict__ is not frame.f_globals
                or frame.f_globals.get("__name__") != "urllib3.util.connection"
                or str(frame.f_globals.get("__file__", "")).replace("\\", "/").casefold()
                != str(self.path).replace("\\", "/").casefold()
                or frame.f_locals.get("host") != "::1"
                or type(frame.f_locals.get("host")) is not str
                or frame.f_globals.get("socket") is not self.socket_module
                or self.socket_module.socket is not self.socket_type
                or self.socket_type.__init__.__code__ is not self.constructor_code
                or self.socket_type.close is not self.close_method):
            self._stop("initialization_binding_rejected")
        self.used = True
        self.frame = frame
        self.counts["function_calls"] = 1

    def _returned(self, frame, result):
        if type(result) is not bool or self.get_ident() != self.thread_id:
            self._stop("abnormal_function_return")
        actual = frame.f_locals.get("sock")
        if self.counts["allowed_bind_attempts"] == 0:
            if result is not False or actual is not None:
                self._stop("unbound_socket_or_result")
            if self.sock is not None and self.native_fileno(self.sock) != -1:
                self._stop("failed_constructor_left_open_socket")
            reason = "no_ipv6" if not self.socket_module.has_ipv6 else "construction_failed"
        else:
            if (actual is not self.sock or type(actual) is not self.socket_type
                    or self.socket_type.close is not self.close_method
                    or self.native_fileno(actual) != -1):
                self._stop("socket_not_closed")
            self.counts["closed_sockets"] = 1
            reason = "bound_and_closed" if result else "bind_failed_and_closed"
        self.finished = True
        self.frame = None
        self._save("completed", reason)
        sys.setprofile(None)

    def _profile(self, frame, event, arg):
        try:
            if event == "call" and frame.f_code.co_name == "_has_ipv6" and (
                frame.f_code.co_filename.replace("\\", "/").casefold()
                == str(self.path).replace("\\", "/").casefold()
            ):
                self._enter(frame)
            elif frame is self.frame and event == "return":
                self._returned(frame, arg)
        except BaseException:
            self._stop("profile_observer_failure")

    def audit(self, event, args, caller):
        """Return True only for the single bound bind; never for other traffic."""
        try:
            if event == "socket.__new__" and self.frame is not None:
                if (self.get_ident() != self.thread_id or sys.getprofile() is not self.callback
                        or caller.f_code is not self.constructor_code
                        or caller.f_back is not self.frame or len(args) != 4
                        or type(args[0]) is not self.socket_type
                        or tuple(args[1:]) != (self.socket_module.AF_INET6,
                                              self.socket_module.SOCK_STREAM, 0)
                        or self.sock is not None):
                    self._stop("socket_creation_rejected")
                self.sock = args[0]
                self.counts["socket_creations"] = 1
            if event != "socket.bind":
                return False
            if (self.frame is None or self.finished or caller is not self.frame
                    or self.get_ident() != self.thread_id or sys.getprofile() is not self.callback
                    or type(args) is not tuple or len(args) != 2
                    or args[0] is not self.sock or self.sock is None
                    or caller.f_locals.get("sock") is not self.sock
                    or type(args[1]) is not tuple or len(args[1]) != 2
                    or type(args[1][0]) is not str or args[1][0] != "::1"
                    or type(args[1][1]) is not int or args[1][1] != 0
                    or (self.sock.family, self.sock.type, self.sock.proto)
                    != (self.socket_module.AF_INET6, self.socket_module.SOCK_STREAM, 0)
                    or self.counts["allowed_bind_attempts"] != 0):
                return False
            self.counts["allowed_bind_attempts"] = 1
            return True
        except BaseException:
            self._stop("audit_observer_failure")




def _credential_environment_name(key):
    name = str(key).upper()
    # This exact numeric configuration key is not an authentication token.
    # Do not generalize to *_TOKENS: every other credential-like name remains
    # denied, including absent keys in the already-cleaned environment.
    if name == "TRADINGAGENTS_LLM_MAX_OUTPUT_TOKENS":
        return False
    return any(part in name for part in ("TOKEN", "API_KEY", "SECRET", "PASSWORD", "CREDENTIAL"))


def _acceptance_denial_context(event, args):
    category = "other"
    if event in {"credential_environment_read", "credential_environment_write"}:
        category = "credential_environment_name"
        if args and args[0] == "TRADINGAGENTS_LLM_MAX_OUTPUT_TOKENS":
            category = "llm_output_limit_setting"
    locations = []
    frame = None
    try:
        frame = sys._getframe(1)
        for _ in range(10):
            if frame is None:
                break
            name = frame.f_code.co_filename.replace("\\", "/")
            label = next((label for suffix, label in (
                ("/scripts/diagnostic_runtime.py", "diagnostic_guard"),
                ("/scripts/accept_financial_statements.py", "acceptance_worker"),
                ("/tradingagents/default_config.py", "default_config"),
                ("/tradingagents/statement_acceptance.py", "statement_acceptance"),
            ) if name.endswith(suffix)), "other")
            if label != "other":
                locations.append({"source": label, "line": min(max(frame.f_lineno, 0), 100000)})
            frame = frame.f_back
    finally:
        del frame
    return {"target_category": category, "call_locations": locations}


class AcceptanceInitializationProbe(Urllib3LoopbackProbe):
    """Pinned import observer for two explicitly authorized acceptance workers.

    The offline-test policy above is unchanged. This observer never replaces
    socket methods: live traffic still needs FinancialNetworkGuard's exact
    connection window, while the reuse worker remains offline.
    """

    POLICY = "statement-acceptance-initialization-v1"

    def __init__(self, root, dependencies, *, role):
        if role not in {"live", "reuse"}:
            raise ValueError("invalid_acceptance_initialization_role")
        super().__init__(root, dependencies, role="cold_child")
        self.role = role
        self.receipt = self.root / (role + "-initialization.json")
        if self.receipt.exists():
            raise RuntimeError("acceptance_initialization_receipt_exists")
        self.phase = "initialization"
        self.expected = None
        self.expected_hit = False
        self.expected_denials = {"socket": 0, "environment": 0, "file": 0}
        self.unexpected_denials = 0
        self.first_unexpected_denial = None

    def start(self):
        if self.used or sys.getprofile() is not None:
            raise RuntimeError("probe_policy_cannot_restart")
        # Install the verified observer before any application import. Unlike
        # the test-only policy, do not permanently disable business sockets.
        sys.setprofile(self.callback)

    def summary(self):
        return {
            "contract": self.POLICY, "role": self.role, "phase": self.phase,
            "implementation_sha256": self.SOURCE_SHA256,
            "version_sha256": self.VERSION_SHA256,
            "counts": dict(self.counts),
            "probe_finished": self.finished,
            "expected_denials": dict(self.expected_denials),
            "unexpected_denials": self.unexpected_denials,
            "first_unexpected_denial": self.first_unexpected_denial,
        }

    def _save(self, status, reason):
        # All reasons are fixed local enums, never exception text or event args.
        data = {**self.summary(), "status": status, "reason": reason}
        self.receipt.write_text(self.json.dumps(data, sort_keys=True), encoding="utf-8")

    def finish_initialization(self):
        if self.phase != "initialization" or not self.used or not self.finished:
            self._stop("acceptance_initialization_incomplete")
        self.phase = "ready"
        self._save("completed", "initialization_complete")

    def denied(self, event, args):
        matched = False
        if self.phase == "self_check" and self.role == "reuse" and not self.expected_hit:
            if self.expected == "socket":
                matched = event == "socket.getaddrinfo" and args == ("offline.invalid", 443, 0, 0, 0)
            elif self.expected == "environment":
                matched = event == "credential_environment_read" and args == ("TUSHARE_TOKEN",)
            elif self.expected == "file" and event == "open" and len(args) == 3:
                path, mode, flags = args
                matched = (
                    isinstance(path, (str, bytes, os.PathLike)) and mode == "r"
                    and type(flags) is int
                    and not flags & (os.O_WRONLY | os.O_RDWR | os.O_CREAT | os.O_TRUNC | os.O_APPEND)
                    and os.path.normcase(os.path.abspath(os.fsdecode(path)))
                    == os.path.normcase(str(self.root / ".env"))
                )
        if matched:
            self.expected_hit = True
            self.expected_denials[self.expected] += 1
            return
        self.unexpected_denials += 1
        if self.first_unexpected_denial is None:
            known = {
                "socket.__new__", "socket.bind", "socket.connect", "socket.getaddrinfo",
                "http.client.connect", "http.client.send", "open", "sqlite3.connect",
                "subprocess.Popen", "os.mkdir", "os.remove", "os.rename", "winreg.OpenKey",
                "credential_environment_read", "credential_environment_write",
                "native_http", "audit_guard_error", "business_exchange_rejected",
                "business_exchange_cleanup_rejected", "business_exchange_suppressed_error",
            }
            self.first_unexpected_denial = {
                "event_category": event if event in known else "other", "phase": self.phase,
                **_acceptance_denial_context(event, args),
            }
        self._stop("unexpected_acceptance_denial")

    def expected_denial(self, name):
        from contextlib import contextmanager

        @contextmanager
        def expected():
            if (self.role != "reuse" or self.phase != "ready"
                    or name not in self.expected_denials or self.expected_denials[name]):
                self._stop("invalid_acceptance_self_check")
            self.phase, self.expected, self.expected_hit = "self_check", name, False
            try:
                yield
            finally:
                self.phase, self.expected = "ready", None
            if not self.expected_hit:
                self._stop("acceptance_self_check_not_blocked")
            self._save("completed", "expected_self_check_blocked")

        return expected()

    def begin_application(self):
        required = {"socket": 1, "environment": 1, "file": 1} if self.role == "reuse" else {
            "socket": 0, "environment": 0, "file": 0
        }
        if self.phase != "ready" or self.expected_denials != required:
            self._stop("acceptance_self_checks_incomplete")
        self.phase = "application"

    def finish_application(self):
        if self.phase != "application":
            self._stop("acceptance_application_phase_mismatch")
        self.phase = "complete"
        self._save("completed", "application_complete")

    def exchange(self, guard, connection, endpoint):
        from contextlib import contextmanager

        @contextmanager
        def permitted():
            if not self.allows_business_transport:
                self.denied("business_exchange_rejected", ())
            try:
                context = guard.exchange(connection, endpoint)
                context.__enter__()
            except Exception:
                self.denied("business_exchange_rejected", ())
            error = None
            try:
                yield
            except BaseException as exc:
                error = (type(exc), exc, exc.__traceback__)
            try:
                suppressed = context.__exit__(*(error or (None, None, None)))
            except Exception:
                self.denied("business_exchange_cleanup_rejected", ())
            if suppressed:
                self.denied("business_exchange_suppressed_error", ())
            if error is not None:
                raise error[1].with_traceback(error[2])

        return permitted()

    @property
    def allows_business_transport(self):
        return self.role == "live" and self.phase == "application"


def install(root, source, *, offline=True, network_guard=None, shared_storage=None,
            test_loopback_probe=None, acceptance_initialization=None, project_env_credentials=False):
    if not (sys.flags.isolated and sys.flags.no_site and sys.dont_write_bytecode):
        raise RuntimeError("diagnostics require -I -S -B")
    root, source = Path(root).resolve(), Path(source).resolve()
    if type(project_env_credentials) is not bool or (project_env_credentials and (
            offline or network_guard is None or shared_storage is None
            or acceptance_initialization != 'live')):
        raise ValueError('explicit_live_project_env_required')
    if shared_storage is not None:
        import _sqlite3
        import sqlite3

        # Like the audit hook, this lasts for the short-lived worker. Protect
        # cache connections as well as the existing counter authority.
        factory = shared_storage.connection_factory(sqlite3.connect)
        sqlite3.connect = sqlite3.dbapi2.connect = _sqlite3.connect = factory
    counts = {"network": 0, "credentials": 0, "subprocess": 0, "outside_write": 0}
    if project_env_credentials:
        counts['project_env_reads'] = 0
    blocked_network = []
    acceptance_probe = None
    clean = safe_environment(root)
    os.environ.clear()
    os.environ.update(clean)

    class Environment(MutableMapping):
        def __getitem__(self, key):
            if _credential_environment_name(key):
                counts["credentials"] += 1
                if acceptance_probe is not None:
                    acceptance_probe.denied("credential_environment_read", (key,))
                raise DeniedAccess("credential environment access prohibited")
            return clean[key]

        def __setitem__(self, key, value):
            if _credential_environment_name(key):
                if acceptance_probe is not None:
                    acceptance_probe.denied("credential_environment_write", (key,))
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
    probe = None
    if acceptance_initialization is not None:
        if (test_loopback_probe is not None or network_guard is None
                or acceptance_initialization not in {"live", "reuse"}
                or offline is not (acceptance_initialization == "reuse")):
            raise ValueError("explicit_acceptance_initialization_required")
        probe = acceptance_probe = AcceptanceInitializationProbe(
            root, dependencies, role=acceptance_initialization
        )
    if test_loopback_probe is not None:
        if (test_loopback_probe != Urllib3LoopbackProbe.POLICY
                or not offline or network_guard is not None):
            raise ValueError("explicit offline test-only probe policy required")
        probe = Urllib3LoopbackProbe(root, dependencies)
        counts["loopback_probe_allowed"] = 0

    def write(path, operation="write"):
        if isinstance(path, (str, bytes, os.PathLike)) and not Path(
            os.fsdecode(path)
        ).resolve().is_relative_to(root):
            if shared_storage is not None and shared_storage.permits(path, operation):
                return
            counts["outside_write"] += 1
            raise DeniedAccess("diagnostic write outside isolated output")

    def guarded_audit(event, args, caller):
        if (probe is not None and event in {"socket.__new__", "socket.bind"}
                and probe.audit(event, args, caller)):
            if acceptance_probe is None:
                counts["loopback_probe_allowed"] += 1
            return
        if acceptance_probe is not None and event == "socket.__new__":
            if acceptance_probe.phase == "initialization":
                if acceptance_probe.frame is None:
                    raise DeniedAccess("unbound initialization socket")
            elif (not acceptance_probe.allows_business_transport
                    or network_guard._connection is None):
                raise DeniedAccess("socket outside accepted transport")
        if event.startswith("winreg."):
            raise DeniedAccess("registry access prohibited")
        if event in {"subprocess.Popen", "os.system", "os.posix_spawn"}:
            counts["subprocess"] += 1
            raise DeniedAccess("worker subprocess prohibited")
        network_event = (
            event.startswith("socket.") and event != "socket.__new__"
        ) or event in {"http.client.connect", "http.client.send"}
        if network_event and (
            offline or (network_guard is not None and not network_guard.allows(event, args))
            or (acceptance_probe is not None and not acceptance_probe.allows_business_transport)
        ):
            counts["network"] += 1
            frame = caller
            blocked_network.append(
                {
                    "event": event,
                    "module": Path(frame.f_code.co_filename).name,
                    "function": frame.f_code.co_name,
                }
            )
            raise DeniedAccess("unplanned diagnostic network prohibited")
        if event == "ctypes.dlopen" and Path(str(args[0])).stem.casefold() in {
            "kernel32",
            "advapi32",
            "webauthn",
            "ncrypt",
            "secur32",
            "user32",
        }:
            frame = caller
            while frame:
                if frame.f_code.co_filename.replace("\\", "/").endswith(
                    ("/colorama/win32.py", "/rich/_win32_console.py")
                ):
                    break
                frame = frame.f_back
            else:
                raise DeniedAccess("native security APIs prohibited")
        if event == "sqlite3.connect" and str(args[0]) != ":memory:":
            if shared_storage is not None:
                path = shared_storage.check_connection_event(args[0])
                write(path)
            else:
                write(args[0])
        if event == "open":
            path, mode, flags = args
            if isinstance(path, (str, bytes, os.PathLike)):
                actual = Path(os.fsdecode(path)).resolve()
                allowed_project_env = False
                if project_env_credentials and actual == source / '.env':
                    allowed_project_env = (
                        caller is not None
                        and Path(caller.f_code.co_filename).resolve() == source / 'scripts/project_credentials.py'
                        and caller.f_code.co_name == '_read_file'
                        and not flags & (os.O_WRONLY | os.O_RDWR | os.O_CREAT | os.O_TRUNC | os.O_APPEND)
                        and counts['project_env_reads'] == 0)
                    if allowed_project_env:
                        counts['project_env_reads'] += 1
                if (
                    (actual.name.startswith(".env") and not allowed_project_env)
                    or actual.name == "test_pytest_persistence_isolation.py"
                    or any(part.lower() in {".ssh", ".aws", ".azure"} for part in actual.parts)
                ):
                    counts["credentials"] += 1
                    raise DeniedAccess("protected file access prohibited")
                if not any(actual.is_relative_to(allowed) for allowed in read_roots) and not (
                    shared_storage is not None and shared_storage.permits(
                        actual, "write" if flags & (os.O_WRONLY | os.O_RDWR | os.O_CREAT
                                                   | os.O_TRUNC | os.O_APPEND) else "read"
                    )
                ):
                    raise DeniedAccess("read outside diagnostic source/runtime")
            if (isinstance(mode, str) and any(c in mode for c in "wax+")) or flags & (
                os.O_WRONLY | os.O_RDWR | os.O_CREAT | os.O_TRUNC | os.O_APPEND
            ):
                write(path)
        if event in {"os.mkdir", "os.remove", "os.rmdir", "os.chmod", "os.utime", "os.truncate"}:
            operation = {"os.mkdir": "mkdir", "os.remove": "remove"}.get(event, event)
            write(args[0], operation)
        if event in {"os.rename", "os.link", "os.symlink"} and not (
            event == "os.link" and shared_storage is not None
            and shared_storage.permits_link(args[0], args[1])
        ):
            write(args[0], event)
            write(args[1], event)

    def audit(event, args):
        caller = None
        try:
            # Frame inspection itself emits an audit event. Only request a frame
            # for operations whose handlers need provenance, never introspection.
            if event.startswith("socket.") or (project_env_credentials and event == 'open') or event in {
                "http.client.connect", "http.client.send", "ctypes.dlopen"
            }:
                caller = sys._getframe(1)
            guarded_audit(event, args, caller)
        except Exception as error:
            if acceptance_probe is not None:
                if isinstance(error, DeniedAccess):
                    acceptance_probe.denied(event, args)
                else:
                    acceptance_probe.denied("audit_guard_error", ())
            raise
        finally:
            del caller

    sys.addaudithook(audit)
    if probe is not None:
        probe.start()
    if offline or network_guard is not None:
        # curl_cffi's native transport does not emit Python socket audit events.
        # Seal that path before application/AKShare imports.
        from curl_cffi import requests as curl_requests

        def no_curl(*args, **kwargs):
            counts["network"] += 1
            if acceptance_probe is not None:
                acceptance_probe.denied("native_http", ())
            raise DeniedAccess("offline native HTTP prohibited")

        curl_requests.Session.request = no_curl
        curl_requests.AsyncSession.request = no_curl
        if probe is not None:
            from curl_cffi import AsyncCurl, Curl
            def stop_native(*args, **kwargs):
                counts["network"] += 1
                probe._stop("forbidden_native_http")
            Curl.perform = stop_native
            AsyncCurl.add_handle = stop_native
    if acceptance_probe is not None:
        return counts, blocked_network, acceptance_probe
    return counts, blocked_network


def _probe_denial_metadata(reason):
    """Fixed diagnostic enums only; no target access or new policy decision."""
    fallback = {"rule_category": "other", "operation": "other", "access": "other",
                "target_category": "other", "target_scope": "other", "stage": "other",
                "call_locations": [{"module": "other", "line": 0}]}
    try:
        phases = {
            "initialization_binding_rejected": "probe_initialization",
            "socket_creation_rejected": "probe_audit",
            "audit_observer_failure": "probe_audit",
            "abnormal_function_return": "probe_return",
            "unbound_socket_or_result": "probe_return",
            "failed_constructor_left_open_socket": "probe_return",
            "socket_not_closed": "probe_return",
            "profile_observer_failure": "probe_profile",
            "forbidden_socket_method": "transport_operation",
            "forbidden_native_http": "transport_operation",
        }
        rule = reason if type(reason) is str and reason in phases else "other"
        operation = ("socket_method" if rule == "forbidden_socket_method" else
                     "native_http" if rule == "forbidden_native_http" else
                     "probe_lifecycle" if rule != "other" else "other")
        access = "network" if phases.get(rule) == "transport_operation" else "inspect" if rule != "other" else "other"
        deps = Path(sys.executable).parent.parent / "Lib/site-packages"
        labels = {str(path).replace(chr(92), "/").casefold(): label for path, label in (
            (Path(__file__), "diagnostic_guard"),
            (deps / "urllib3/util/connection.py", "urllib3_connection"),
            (Path(sys.base_prefix) / "Lib/socket.py", "stdlib_socket"),
            (Path(sys.base_prefix) / "Lib/ssl.py", "stdlib_ssl"),
        )}
        locations = []
        frame = sys._getframe(1)
        for _ in range(8):
            if frame is None:
                break
            label = labels.get(frame.f_code.co_filename.replace(chr(92), "/").casefold(), "other")
            line = min(max(frame.f_lineno, 0), 100000) if label != "other" else 0
            locations.append({"module": label, "line": line})
            frame = frame.f_back
        return {"rule_category": rule, "operation": operation, "access": access,
                "target_category": "network_socket" if rule != "other" else "other",
                "target_scope": "other", "stage": phases.get(rule, "other"),
                "call_locations": locations}
    except BaseException:
        return fallback
    finally:
        frame = None

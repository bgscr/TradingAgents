"""Synthetic stores and HTTPS only; never inspect an operator's shared store."""
import copy
import json
import os
import sqlite3
import subprocess
import sys
from contextlib import closing
from datetime import datetime, timedelta, timezone
from hashlib import sha256
from pathlib import Path

import pytest

from scripts.diagnostic_runtime import SharedStatementStorage, safe_environment
from tests.test_financial_acceptance_scope import fake_https, sample_rows
from tradingagents import statement_acceptance as acceptance
from tradingagents.dataflows.provider_subrequests import ProviderSubrequestInstrumentIdentity
from tradingagents.market_history.config import (
    MarketHistoryConfig,
    provider_request_authority_config,
)
from tradingagents.market_history.coordinator import (
    ProviderRequestCoordinator,
    upstream_service_identity_for_provider,
)
from tradingagents.market_history.store import MarketHistoryStore

REQUEST = {"ticker": "601328.SH", "destination": "https://api.waditu.com/dataapi",
           "account": "synthetic-account", "as_of": "2026-10-16"}


@pytest.fixture
def shared(tmp_path):
    base = tmp_path / "shared"
    base.mkdir()
    config, asset = acceptance.validate_request(base, **REQUEST)
    history = MarketHistoryConfig.from_mapping(config)
    with MarketHistoryStore.open(history):
        pass
    authority = provider_request_authority_config(history)
    with MarketHistoryStore.open_provider_request_authority(history) as store:
        coordinator = ProviderRequestCoordinator(store)
        upstream_id, service = upstream_service_identity_for_provider("tushare")
        coordinator.register_upstream_service(upstream_id, service)
        coordinator.configure_operator_ceiling(
            upstream_service_id=upstream_id, minimum_interval=timedelta(seconds=1.5),
            allow_prewarming=False, configured_at=datetime.now(timezone.utc),
        )
    cache = base / "cache"
    cache.mkdir()
    stat = authority.database_path.stat()
    binding = {
        "contract": "shared-statement-acceptance-v1", "account": REQUEST["account"],
        "destination": REQUEST["destination"], "as_of": REQUEST["as_of"],
        "announcement_start": "2020-01-01", "announcement_end": "2026-10-16",
        "normalizer_version": "current-tushare-statements-v1",
        "instrument_identity_revision": ProviderSubrequestInstrumentIdentity.from_authoritative(
            asset.instrument_identity
        ).identity_revision,
        "storage": {"market_database": str(history.database_path),
                    "coordinator_database": str(authority.database_path),
                    "cache_database": str(cache / "current-financial-statements.sqlite3"),
                    "coordinator_file_id": [stat.st_dev, stat.st_ino]},
    }
    return binding, SharedStatementStorage(binding["storage"])


def count(storage):
    with closing(sqlite3.connect(storage.coordinator)) as db:
        return db.execute("SELECT COUNT(*) FROM provider_request_attempts").fetchone()[0]


def test_stricter_shared_ceiling_is_retained_verbatim(tmp_path, monkeypatch, shared):
    binding, storage = shared
    calls = fake_https(monkeypatch, sample_rows)
    with closing(sqlite3.connect(storage.coordinator)) as db, db:
        raw = db.execute("SELECT operator_ceiling_json FROM upstream_services").fetchone()[0]
        policy = json.loads(raw)
        policy["minimum_interval_seconds"] = 6.0
        retained = json.dumps(policy)
        db.execute("UPDATE upstream_services SET operator_ceiling_json=?", (retained,))
    original_configure = ProviderRequestCoordinator.configure_operator_ceiling
    result = acceptance.acquire(tmp_path / "run", **REQUEST, token="synthetic-only",
                                shared_binding=binding, shared_storage=storage)
    assert result["status"] == "acquired" and len(calls) == 3
    assert ProviderRequestCoordinator.configure_operator_ceiling is original_configure
    with closing(sqlite3.connect(storage.coordinator)) as db:
        assert db.execute("SELECT operator_ceiling_json FROM upstream_services").fetchone()[0] == retained
    assert count(storage) == 3


@pytest.mark.parametrize("change", ["missing", "weaker", "prewarming"])
def test_incompatible_ceiling_rejected_before_request(tmp_path, monkeypatch, shared, change):
    binding, storage = shared
    calls = fake_https(monkeypatch, sample_rows)
    with closing(sqlite3.connect(storage.coordinator)) as db, db:
        policy = json.loads(db.execute("SELECT operator_ceiling_json FROM upstream_services").fetchone()[0])
        if change == "weaker":
            policy["minimum_interval_seconds"] = 1.0
        if change == "prewarming":
            policy["allow_prewarming"] = True
        db.execute("UPDATE upstream_services SET operator_ceiling_json=?",
                   (None if change == "missing" else json.dumps(policy),))
    with pytest.raises(ValueError):
        acceptance.acquire(tmp_path / "run", **REQUEST, token="synthetic-only",
                           shared_binding=binding, shared_storage=storage)
    assert calls == [] and count(storage) == 0


def test_real_dispatch_reuses_shared_counts_and_cache(tmp_path, monkeypatch, shared):
    binding, storage = shared
    calls = fake_https(monkeypatch, sample_rows)
    # First use a different cutoff to populate real synthetic coordinator history.
    first = acceptance.acquire(storage.market.parent, token="synthetic-only",
                               **{**REQUEST, "as_of": "2026-10-15"})
    assert first["status"] == "acquired"
    before = count(storage)
    assert before == 3
    result = acceptance.acquire(tmp_path / "new-run", token="synthetic-only", **REQUEST,
                                shared_binding=binding, shared_storage=storage)
    assert result["status"] == "acquired"
    assert result["physical_requests"] == 3
    assert count(storage) == before + 3
    reused = acceptance.reuse(tmp_path / "other-run", **REQUEST,
                              shared_binding=binding, shared_storage=storage)
    assert reused["status"] == "reused"
    assert reused["views"] == result["views"]
    assert reused["physical_requests"] == 0
    assert count(storage) == 6 and len(calls) == 6
    assert not (tmp_path / "new-run" / "coordinator.sqlite3").exists()


@pytest.mark.parametrize("key,value", [
    ("account", "other"), ("destination", "https://api.tushare.pro"),
    ("instrument_identity_revision", "wrong"), ("as_of", "2026-10-17"),
    ("announcement_start", "2025-01-01"), ("announcement_end", "2026-10-17"),
    ("normalizer_version", "other"), ("contract", "other"), ("extra", "unexpected"),
])
def test_request_mismatch_before_client(tmp_path, monkeypatch, shared, key, value):
    binding, storage = shared
    changed = {**binding, key: value}
    calls = fake_https(monkeypatch, sample_rows)
    with pytest.raises(ValueError):
        acceptance.acquire(tmp_path / "run", **REQUEST, token="synthetic-only",
                           shared_binding=changed, shared_storage=storage)
    assert not calls and count(storage) == 0


@pytest.mark.parametrize("change", ["missing", "empty", "id", "derived", "owner", "schema", "import", "service"])
def test_missing_or_mismatched_authority_fails_closed(tmp_path, shared, change):
    binding, storage = shared
    changed = copy.deepcopy(binding)
    if change == "missing":
        storage.coordinator.unlink()
    elif change == "empty":
        storage.coordinator.write_bytes(b"")
    elif change == "id":
        changed["storage"]["coordinator_file_id"][1] += 1
    elif change == "derived":
        changed["storage"]["coordinator_database"] = str(tmp_path / "other.sqlite3")
    elif change == "owner":
        storage.owner.write_text("different-owner\n", encoding="utf-8")
    else:
        with closing(sqlite3.connect(storage.coordinator)) as db, db:
            db.execute({"schema": "DELETE FROM schema_migrations WHERE version=9",
                        "import": "DELETE FROM provider_request_state_imports",
                        "service": "DELETE FROM upstream_services"}[change])
    with pytest.raises(ValueError):
        selected = SharedStatementStorage(changed["storage"])
        acceptance.validate_request(tmp_path / "run", **REQUEST,
                                    shared_binding=changed, shared_storage=selected)
    if change == "missing":
        assert not storage.coordinator.exists()


def test_path_policy_is_exact(shared, tmp_path):
    _, storage = shared
    assert storage.permits(storage.cache, "write")
    assert storage.permits(Path(str(storage.coordinator) + "-wal"), "write")
    assert storage.permits(storage.owner_dir / (storage.OWNER + "." + "a" * 32 + ".tmp"), "write")
    for path in (storage.cache.parent / "other.sqlite3", storage.market,
                 storage.owner_dir / "arbitrary", tmp_path / "elsewhere"):
        assert not storage.permits(path, "write")
    assert not storage.permits(storage.coordinator, "remove")
    assert not storage.permits(storage.owner, "write")
    assert not storage.permits(storage.cache.parent / "newdir", "mkdir")
    with pytest.raises(ValueError):
        storage.permits(storage.cache.parent / ".." / "escape", "write")
    linked = storage.cache.parent / "hardlink"
    os.link(storage.coordinator, linked)
    with pytest.raises(ValueError):
        storage.check_existing()
    linked.unlink()


def test_rw_open_cannot_create_or_attach(shared, tmp_path):
    import _sqlite3

    _, storage = shared
    with storage.connection_scope():
        for factory in (sqlite3.connect, sqlite3.dbapi2.connect, _sqlite3.connect):
            for target in (storage.cache, storage.coordinator):
                with closing(factory(target)) as db:
                    for sql in ("ATTACH DATABASE ? AS other", "VACUUM INTO ?"):
                        with pytest.raises(sqlite3.DatabaseError):
                            db.execute(sql, (str(tmp_path / "native-escape.sqlite3"),))
        with pytest.raises(ValueError):
            sqlite3.connect(storage.cache, factory=sqlite3.Connection)
        with closing(sqlite3.connect(storage.coordinator)) as db, pytest.raises(sqlite3.DatabaseError):
            db.execute("ATTACH DATABASE ? AS other", (str(tmp_path / "unexpected.sqlite3"),))
        storage.coordinator.unlink()
        with pytest.raises(ValueError):
            sqlite3.connect(storage.coordinator)
    assert not storage.coordinator.exists()
    assert not (tmp_path / "unexpected.sqlite3").exists()
    assert not (tmp_path / "native-escape.sqlite3").exists()


@pytest.mark.parametrize("bad", [{"has_more": True, "total": 2}, {"total": 2},
                                 {"has_more": False, "total": 3}])
def test_unknown_or_truncated_response_stops_next_table(tmp_path, monkeypatch, shared, bad):
    binding, storage = shared
    frame = sample_rows("balancesheet", "601328.SH")
    calls = fake_https(monkeypatch, sample_rows, response={"code": 0, "data": {
        "fields": list(frame.columns), "items": frame.values.tolist(), **bad}})
    result = acceptance.acquire(tmp_path / "run", **REQUEST, token="synthetic-only",
                                shared_binding=binding, shared_storage=storage)
    # A matched row total is accepted as query-response evidence by the
    # installed compatibility contract. This fixture then supplies the SAME
    # balance-sheet columns for income, which must fail there without cashflow.
    expected = ["balancesheet", "income"] if bad == {"total": 2} else ["balancesheet"]
    assert result["status"] == "stopped" and result["physical_requests"] == len(expected)
    assert [call["api_name"] for call in calls] == expected
    assert count(storage) == len(expected)
    assert result['completed_tools'] == (["get_balance_sheet"] if len(expected) == 2 else [])
    assert result['failed_view'] == ("get_income_statement:quarterly" if len(expected) == 2 else "get_balance_sheet:quarterly")


def test_cli_binding_document_closed_and_request_bound(tmp_path, shared):
    from scripts.accept_financial_statements import load_shared_binding

    binding, _ = shared
    path = tmp_path / "binding.json"
    path.write_text(json.dumps(binding), encoding="utf-8")
    assert load_shared_binding(path, **REQUEST) == binding
    path.write_text(json.dumps({**binding, "unknown": "not-retained"}), encoding="utf-8")
    with pytest.raises(ValueError):
        load_shared_binding(path, **REQUEST)


def test_real_guard_shared_sqlite_and_other_writes_denied(tmp_path, shared):
    binding, parent_storage = shared
    root = tmp_path / "guard-output"
    root.mkdir()
    source = Path(acceptance.__file__).resolve().parents[1]
    packet = root / "synthetic-binding.json"
    packet.write_text(json.dumps(binding), encoding="utf-8")
    denied_paths = (parent_storage.cache.parent / "unrelated", parent_storage.owner,
                    parent_storage.market)
    before_denied = {path: sha256(path.read_bytes()).hexdigest() if path.exists() else None
                     for path in denied_paths}
    # Existing diagnostic guard, real audit hooks, fresh interpreter. No historical
    # runner, product qualification, real databases or network permission involved.
    code = '''
import _sqlite3, json, os, pathlib, sqlite3, sys
from contextlib import closing
source, root = map(pathlib.Path, sys.argv[1:])
sys.path.insert(0, str(source))
from scripts.diagnostic_runtime import SharedStatementStorage, install
from scripts import diagnostic_runtime as guard
binding = json.loads((root / "synthetic-binding.json").read_text())
storage = SharedStatementStorage(binding["storage"])
saved_connect = _sqlite3.connect
guard_file = guard.__file__
import types
audit_codes = [value for value in guard.install.__code__.co_consts
               if isinstance(value, types.CodeType) and value.co_name == 'audit']
assert len(audit_codes) == 1
guard_audit_code = audit_codes[0]
stage = 'guard_install'
audit_hook_bound = False
original_addaudithook = sys.addaudithook
expected_operation = None
expected_path = None
expected_exception = None

def register_observable_guard(hook):
    # CPython omits audit callbacks from tracing unless __cantrace__ is true.
    # Enable visibility only for the exact function in the hash-bound source.
    global audit_hook_bound
    code = getattr(hook, '__code__', None)
    if code is guard_audit_code and code.co_filename == guard_file:
        assert not audit_hook_bound
        hook.__cantrace__ = True
        audit_hook_bound = True
    original_addaudithook(hook)

def finish(receipt, code):
    # Exit directly: neither caught guard exceptions nor finally blocks continue.
    sys.settrace(None)
    try:
        with (root / 'guard-denial.json').open('x', encoding='utf-8') as out:
            json.dump(receipt, out, sort_keys=True)
            out.flush()
    finally:
        os._exit(code)

def observe(frame, event, arg):
    global expected_exception
    if event != 'exception' or frame.f_code.co_filename != guard_file:
        return observe
    if not isinstance(arg[1], guard.DeniedAccess):
        return observe
    # Only the exact deliberately exercised storage exception may propagate.
    # Zero counters alone never make an unexpected denial acceptable.
    if arg[1] is expected_exception:
        return observe
    if (expected_operation == 'unbound_sqlite'
            and frame.f_code.co_name == 'check_connection_event'
            and frame.f_locals.get('path') == expected_path
            and str(arg[1]) == 'shared SQLite connection must use bound factory'):
        expected_exception = arg[1]
        return observe
    call_args = frame.f_locals.get('args')
    if (expected_operation == 'outside_open' and frame.f_code.co_name == 'audit'
            and frame.f_locals.get('event') == 'open'
            and type(call_args) is tuple and len(call_args) == 3
            and call_args[0] == str(expected_path)
            and type(call_args[2]) is int
            and call_args[2] & (os.O_WRONLY | os.O_RDWR | os.O_CREAT | os.O_TRUNC)
            and str(arg[1]) == 'read outside diagnostic source/runtime'):
        expected_exception = arg[1]
        return observe
    kinds = {'socket.bind', 'socket.connect', 'socket.getaddrinfo', 'socket.gethostname',
             'socket.gethostbyname', 'socket.gethostbyaddr', 'socket.getnameinfo',
             'socket.sendto', 'http.client.connect', 'http.client.send',
             'subprocess.Popen', 'os.system', 'os.posix_spawn', 'open', 'sqlite3.connect'}
    functions = {'audit', 'write', '__getitem__', '__setitem__', 'no_curl',
                 'check_connection_event', '_has_ipv6', 'create_connection', 'connect',
                 '_create_connection', 'getaddrinfo', 'gethostname'}
    modules = {'scripts/diagnostic_runtime.py', 'urllib3/util/connection.py',
               'curl_cffi/requests/session.py', 'curl_cffi/requests/__init__.py',
               'pandas/compat/__init__.py', 'socket.py', 'platform.py'}
    frames = []
    current = frame
    for _ in range(8):
        if current is None:
            break
        filename = current.f_code.co_filename.replace('\\\\', '/')
        module = next((name for name in sorted(modules) if filename.endswith('/' + name)), 'other')
        name = current.f_code.co_name
        frames.append({'module': module, 'function': name if name in functions else 'other',
                       'line': min(max(current.f_lineno, 0), 100000)})
        current = current.f_back
    raw_event = frame.f_locals.get('event')
    category = raw_event if raw_event in kinds else 'other_guard_denial'
    target = 'not_recorded'
    if category == 'socket.bind':
        args = frame.f_locals.get('args')
        address = args[1] if type(args) is tuple and len(args) == 2 else None
        target = ('loopback_ephemeral' if type(address) is tuple and len(address) >= 2
                  and address[0] in ('::1', '127.0.0.1') and address[1] == 0
                  else 'address_redacted')
    elif category == 'socket.gethostname':
        target = 'local_hostname'
    counts = frame.f_locals.get('counts', {})
    safe_counts = {key: min(value, 65535) for key in
                   ('network', 'credentials', 'subprocess', 'outside_write')
                   if type(value := counts.get(key)) is int and value >= 0}
    finish({'contract': 'first-guard-denial-v1', 'status': 'stopped_at_guard_denial',
            'stage': stage, 'exception': 'DeniedAccess', 'event': category,
            'target_category': target, 'frames': frames, 'counts_capped_at_65535': safe_counts,
            'audit_hook_bound': audit_hook_bound,
            'storage_checks_started': stage == 'storage_validation',
            'denied_operation_continued': False}, 73)

sys.settrace(observe)
sys.addaudithook = register_observable_guard
counts, _ = install(root, source, shared_storage=storage)
sys.addaudithook = original_addaudithook
assert audit_hook_bound
stage = 'statement_acceptance_import'
from tradingagents.statement_acceptance import validate_request
from tradingagents.market_history.config import MarketHistoryConfig
from tradingagents.market_history.store import MarketHistoryStore
assert "tradingagents.dataflows.financial_dispatch" not in sys.modules
assert "requests" not in sys.modules and "urllib3" not in sys.modules
stage = "storage_validation"
config, _ = validate_request(root, ticker="601328.SH", account="synthetic-account",
    destination="https://api.waditu.com/dataapi", as_of="2026-10-16",
    shared_binding=binding, shared_storage=storage)
with storage.connection_scope():
    with MarketHistoryStore.open_provider_request_authority(MarketHistoryConfig.from_mapping(config)):
        pass
for factory in (sqlite3.connect, sqlite3.dbapi2.connect, _sqlite3.connect):
    with closing(factory(storage.cache)) as db:
        db.execute("CREATE TABLE IF NOT EXISTS synthetic (id INTEGER)")
        db.commit()
        for sql in ("ATTACH DATABASE ? AS other", "VACUUM INTO ?"):
            try:
                db.execute(sql, (str(storage.cache.parent / "native-escape.sqlite3"),))
            except sqlite3.DatabaseError:
                pass
            else:
                raise AssertionError("native path escape accepted")
for unbound in (saved_connect, sqlite3.Connection):
    expected_operation, expected_path, expected_exception = 'unbound_sqlite', storage.cache, None
    try:
        unbound(storage.cache)
    except guard.DeniedAccess as exc:
        assert str(exc) == 'shared SQLite connection must use bound factory'
    else:
        raise AssertionError("unbound shared connection accepted")
    finally:
        expected_operation = expected_path = expected_exception = None
blocked = []
for path in (storage.cache.parent / "unrelated", storage.owner, storage.market):
    expected_operation, expected_path, expected_exception = 'outside_open', path, None
    try:
        path.write_text("synthetic")
    except guard.DeniedAccess as exc:
        assert str(exc) == 'read outside diagnostic source/runtime'
        blocked.append(path.name)
    else:
        raise AssertionError("outside write accepted")
    finally:
        expected_operation = expected_path = expected_exception = None
assert len(blocked) == 3
assert counts["network"] == counts["credentials"] == counts["subprocess"] == 0
(root / "result.json").write_text(json.dumps({"blocked": len(blocked), "counts": counts}))
'''
    result = subprocess.run([sys.executable, "-I", "-S", "-B", "-c", code, str(source), str(root)],
                            cwd=root, env=safe_environment(root), capture_output=True,
                            text=True, timeout=60, check=False)
    after_denied = {path: sha256(path.read_bytes()).hexdigest() if path.exists() else None
                    for path in denied_paths}
    assert after_denied == before_denied
    assert result.returncode == 0, result.stderr
    receipt = json.loads((root / "result.json").read_text())
    # These opens fail the scope check before the write-counter branch.
    assert receipt["blocked"] == 3 and receipt["counts"]["outside_write"] == 0


def test_acceptance_enums_keep_contract_identity():
    from tradingagents.dataflows.financial_contracts import (
        FinancialReportingFrequency,
        FinancialStatementType,
    )

    assert acceptance.FinancialReportingFrequency is FinancialReportingFrequency
    assert acceptance.FinancialStatementType is FinancialStatementType


def test_real_guard_shared_sqlite_with_pinned_loopback_probe(tmp_path, shared):
    binding, parent_storage = shared
    root = tmp_path / "guard-output"
    root.mkdir()
    source = Path(acceptance.__file__).resolve().parents[1]
    packet = root / "synthetic-binding.json"
    packet.write_text(json.dumps(binding), encoding="utf-8")
    denied_paths = (parent_storage.cache.parent / "unrelated", parent_storage.owner,
                    parent_storage.market)
    before_denied = {path: sha256(path.read_bytes()).hexdigest() if path.exists() else None
                     for path in denied_paths}
    # Existing diagnostic guard, real audit hooks, fresh interpreter. No historical
    # runner, product qualification, real databases or network permission involved.
    code = '''
import _sqlite3, json, os, pathlib, sqlite3, sys
from contextlib import closing
source, root = map(pathlib.Path, sys.argv[1:])
sys.path.insert(0, str(source))
from scripts.diagnostic_runtime import SharedStatementStorage, install
from scripts import diagnostic_runtime as guard
binding = json.loads((root / "synthetic-binding.json").read_text())
storage = SharedStatementStorage(binding["storage"])
saved_connect = _sqlite3.connect
guard_file = guard.__file__
import types
audit_codes = [value for value in guard.install.__code__.co_consts
               if isinstance(value, types.CodeType) and value.co_name == 'audit']
assert len(audit_codes) == 1
guard_audit_code = audit_codes[0]
stage = 'guard_install'
audit_hook_bound = False
original_addaudithook = sys.addaudithook
expected_operation = None
expected_path = None
expected_exception = None

def register_observable_guard(hook):
    # CPython omits audit callbacks from tracing unless __cantrace__ is true.
    # Enable visibility only for the exact function in the hash-bound source.
    global audit_hook_bound
    code = getattr(hook, '__code__', None)
    if code is guard_audit_code and code.co_filename == guard_file:
        assert not audit_hook_bound
        hook.__cantrace__ = True
        audit_hook_bound = True
    original_addaudithook(hook)

def finish(receipt, code):
    # Exit directly: neither caught guard exceptions nor finally blocks continue.
    sys.settrace(None)
    try:
        with (root / 'guard-denial.json').open('x', encoding='utf-8') as out:
            json.dump(receipt, out, sort_keys=True)
            out.flush()
    finally:
        os._exit(code)

Path = pathlib.Path
ROOT, MAIN = root, source
DEPS = Path(sys.executable).resolve().parents[1] / "Lib/site-packages"
location_labels = {str(path).replace(chr(92), "/").casefold(): label for path, label in (
    (Path(guard_file), "diagnostic_guard"),
    (MAIN / "tradingagents/default_config.py", "default_config"),
    (MAIN / "tests/conftest.py", "pytest_conftest"),
    (DEPS / "urllib3/util/connection.py", "urllib3_connection"),
    (DEPS / "curl_cffi/requests/session.py", "native_http_session"),
    (DEPS / "_pytest/assertion/rewrite.py", "pytest_assertion_rewrite"),
    (DEPS / "_pytest/capture.py", "pytest_capture"),
    (DEPS / "_pytest/logging.py", "pytest_logging"),
    (DEPS / "_pytest/junitxml.py", "pytest_junitxml"),
    (DEPS / "_pytest/cacheprovider.py", "pytest_cacheprovider"),
    (DEPS / "_pytest/tmpdir.py", "pytest_tmpdir"),
    (DEPS / "_pytest/config/__init__.py", "pytest_config"),
    (DEPS / "_pytest/pathlib.py", "pytest_pathlib"),
    (Path(sys.base_prefix) / "Lib/logging/__init__.py", "stdlib_logging"),
    (Path(sys.base_prefix) / "Lib/tempfile.py", "stdlib_tempfile"),
    (MAIN / "tests/test_shared_statement_binding.py", "test_shared_binding"),
    (MAIN / "tests/test_financial_acceptance_scope.py", "test_acceptance_scope"),
    (MAIN / "tests/test_current_tushare_statements.py", "test_current_statements"),
    (MAIN / "tests/test_tushare_financial_https.py", "test_financial_https"),
)}
def safe_target(value):
    if type(value) is not str and not isinstance(value, Path):
        return "other", "other"
    path = Path(value)
    parts = tuple(part.casefold() for part in path.parts)
    name = path.name.casefold()
    if name.startswith(".env"):
        kind = "dotenv"
    elif name == "tradingagents.local.json":
        kind = "local_profile"
    elif name == "test_pytest_persistence_isolation.py":
        kind = "protected_test"
    elif any(part in {".ssh", ".aws", ".azure"} for part in parts):
        kind = "credential_directory"
    elif path.suffix.casefold() == ".pyc" or "__pycache__" in parts:
        kind = "python_bytecode"
    elif ".pytest_cache" in parts:
        kind = "pytest_cache"
    elif path.suffix.casefold() == ".log":
        kind = "log"
    elif name == "results.xml":
        kind = "pytest_xml"
    elif path.suffix.casefold() in {".sqlite3", ".sqlite", ".db"}:
        kind = "database"
    elif name in {"nul", "/dev/null"}:
        kind = "null_device"
    else:
        kind = "other"
    scope = "other"
    if path.is_absolute() and ".." not in path.parts:
        if path.is_relative_to(ROOT):
            scope = "run_root_lexical"
        elif path.is_relative_to(MAIN):
            scope = "project_root_lexical"
        elif path.is_relative_to(Path(sys.base_prefix)):
            scope = "python_runtime_lexical"
        else:
            scope = "outside_known_roots_lexical"
    return kind, scope


def safe_locations(frame):
    locations = []
    for _ in range(8):
        if frame is None:
            break
        filename = frame.f_code.co_filename.replace(chr(92), "/").casefold()
        label = location_labels.get(filename, "other")
        line = min(max(frame.f_lineno, 0), 100000) if label != "other" else 0
        locations.append({"module": label, "line": line})
        frame = frame.f_back
    return locations


def denial_metadata(frame, error):
    rules = {
        "shared SQLite connection must use bound factory": "shared_database_factory",
        "credential environment access prohibited": "credential_environment_read",
        "credential environment mutation prohibited": "credential_environment_write",
        "diagnostic write outside isolated output": "outside_write",
        "registry access prohibited": "registry_access",
        "worker subprocess prohibited": "subprocess",
        "unplanned diagnostic network prohibited": "network",
        "native security APIs prohibited": "native_security_api",
        "protected file access prohibited": "protected_file",
        "read outside diagnostic source/runtime": "outside_read",
        "offline native HTTP prohibited": "native_http",
    }
    reason = error.args[0] if type(error) is guard.DeniedAccess and len(error.args) == 1 and type(error.args[0]) is str else None
    rule = rules.get(reason, "other")
    op, access, target = "other", "other", None
    current = frame
    for _ in range(8):
        if current is None:
            break
        if current.f_code.co_filename == guard_file and current.f_code.co_name == "audit":
            event = current.f_locals.get("event")
            args = current.f_locals.get("args")
            events = {"open", "sqlite3.connect", "subprocess.Popen", "os.system", "os.posix_spawn",
                "os.mkdir", "os.remove", "os.rmdir", "os.chmod", "os.utime", "os.truncate",
                "os.rename", "os.link", "os.symlink", "socket.bind", "socket.connect",
                "socket.getaddrinfo", "socket.gethostname", "socket.gethostbyname",
                "socket.gethostbyaddr", "socket.getnameinfo", "socket.sendto", "socket.sendmsg",
                "http.client.connect", "http.client.send", "ctypes.dlopen"}
            op = event if event in events else ("registry" if type(event) is str and event.startswith("winreg.") else "other")
            if op == "open" and type(args) is tuple and len(args) == 3:
                target = current.f_locals.get("actual", args[0])
                flags = args[2]
                if type(flags) is int:
                    access = "read_write" if flags & os.O_RDWR else ("write" if flags & (os.O_WRONLY | os.O_CREAT | os.O_TRUNC | os.O_APPEND) else "read")
            elif op in {"sqlite3.connect", "os.mkdir", "os.remove", "os.rmdir", "os.chmod", "os.utime", "os.truncate", "os.rename", "os.link", "os.symlink"} and type(args) is tuple and args:
                target = frame.f_locals.get("path", args[0])
                access = "database" if op == "sqlite3.connect" else "mutate"
            break
        current = current.f_back
    if rule.startswith("credential_environment_"):
        op, access = "environment", "read" if rule.endswith("read") else "write"
    elif rule == "subprocess":
        access = "execute"
    elif rule in {"network", "native_http"}:
        access = "network"
        if rule == "native_http": op = "native_http"
    elif rule in {"registry_access", "native_security_api"}:
        access = "inspect"
    elif rule == "shared_database_factory":
        op, access = "sqlite3.connect", "database"
    kind, scope = safe_target(target)
    return {"rule_category": rule, "operation": op, "access": access,
        "target_category": kind, "target_scope": scope,
        "stage": stage if stage in {"guard_install", "statement_acceptance_import", "storage_validation"} else "other",
        "call_locations": safe_locations(frame)}


def observe(frame, event, arg):
    global expected_exception
    if event != 'exception' or frame.f_code.co_filename != guard_file:
        return observe
    if not isinstance(arg[1], guard.DeniedAccess):
        return observe
    # Only the exact deliberately exercised storage exception may propagate.
    # Zero counters alone never make an unexpected denial acceptable.
    if arg[1] is expected_exception:
        return observe
    if (expected_operation == 'unbound_sqlite'
            and frame.f_code.co_name == 'check_connection_event'
            and frame.f_locals.get('path') == expected_path
            and str(arg[1]) == 'shared SQLite connection must use bound factory'):
        expected_exception = arg[1]
        return observe
    call_args = frame.f_locals.get('args')
    if (expected_operation == 'outside_open' and frame.f_code.co_name == 'audit'
            and frame.f_locals.get('event') == 'open'
            and type(call_args) is tuple and len(call_args) == 3
            and call_args[0] == str(expected_path)
            and type(call_args[2]) is int
            and call_args[2] & (os.O_WRONLY | os.O_RDWR | os.O_CREAT | os.O_TRUNC)
            and str(arg[1]) == 'read outside diagnostic source/runtime'):
        expected_exception = arg[1]
        return observe
    try:
        metadata = denial_metadata(frame, arg[1])
    except BaseException:
        metadata = {"rule_category": "other", "operation": "other", "access": "other",
            "target_category": "other", "target_scope": "other", "stage": "other",
            "call_locations": [{"module": "other", "line": 0}]}
    counts = frame.f_locals.get('counts', {})
    safe_counts = {key: min(value, 65535) for key in
                   ('network', 'credentials', 'subprocess', 'outside_write')
                   if type(value := counts.get(key)) is int and value >= 0}
    finish({'contract': 'first-guard-denial-v1', 'status': 'stopped_at_guard_denial',
            **metadata, 'exception': 'DeniedAccess', 'event': metadata['operation'],
            'frames': metadata['call_locations'], 'counts_capped_at_65535': safe_counts,
            'audit_hook_bound': audit_hook_bound,
            'storage_checks_started': stage == 'storage_validation',
            'denied_operation_continued': False}, 73)

sys.settrace(observe)
sys.addaudithook = register_observable_guard
counts, _ = install(root, source, shared_storage=storage,
                    test_loopback_probe=guard.Urllib3LoopbackProbe.POLICY)
sys.addaudithook = original_addaudithook
os.environ["TRADINGAGENTS_OFFLINE_TEST_DISABLE_LOCAL_PROFILE"] = "1"
assert audit_hook_bound
stage = 'statement_acceptance_import'
from tradingagents.statement_acceptance import validate_request
from tradingagents.market_history.config import MarketHistoryConfig
from tradingagents.market_history.store import MarketHistoryStore
assert "tradingagents.dataflows.financial_dispatch" not in sys.modules
probe_receipt = json.loads((root / "loopback-probe-policy.json").read_text())
assert probe_receipt["contract"] == guard.Urllib3LoopbackProbe.POLICY
assert probe_receipt["implementation_sha256"] == guard.Urllib3LoopbackProbe.SOURCE_SHA256
assert probe_receipt["status"] == "completed"
assert probe_receipt["counts"]["function_calls"] == 1
assert probe_receipt["counts"]["socket_creations"] == 1
assert probe_receipt["counts"]["allowed_bind_attempts"] == 1
assert probe_receipt["counts"]["closed_sockets"] == 1
assert counts["loopback_probe_allowed"] == 1
assert sys.getprofile() is None
stage = "storage_validation"
config, _ = validate_request(root, ticker="601328.SH", account="synthetic-account",
    destination="https://api.waditu.com/dataapi", as_of="2026-10-16",
    shared_binding=binding, shared_storage=storage)
with storage.connection_scope():
    with MarketHistoryStore.open_provider_request_authority(MarketHistoryConfig.from_mapping(config)):
        pass
for factory in (sqlite3.connect, sqlite3.dbapi2.connect, _sqlite3.connect):
    with closing(factory(storage.cache)) as db:
        db.execute("CREATE TABLE IF NOT EXISTS synthetic (id INTEGER)")
        db.commit()
        for sql in ("ATTACH DATABASE ? AS other", "VACUUM INTO ?"):
            try:
                db.execute(sql, (str(storage.cache.parent / "native-escape.sqlite3"),))
            except sqlite3.DatabaseError:
                pass
            else:
                raise AssertionError("native path escape accepted")
for unbound in (saved_connect, sqlite3.Connection):
    expected_operation, expected_path, expected_exception = 'unbound_sqlite', storage.cache, None
    try:
        unbound(storage.cache)
    except guard.DeniedAccess as exc:
        assert str(exc) == 'shared SQLite connection must use bound factory'
    else:
        raise AssertionError("unbound shared connection accepted")
    finally:
        expected_operation = expected_path = expected_exception = None
blocked = []
for path in (storage.cache.parent / "unrelated", storage.owner, storage.market):
    expected_operation, expected_path, expected_exception = 'outside_open', path, None
    try:
        path.write_text("synthetic")
    except guard.DeniedAccess as exc:
        assert str(exc) == 'read outside diagnostic source/runtime'
        blocked.append(path.name)
    else:
        raise AssertionError("outside write accepted")
    finally:
        expected_operation = expected_path = expected_exception = None
assert len(blocked) == 3
assert counts["network"] == counts["credentials"] == counts["subprocess"] == 0
(root / "result.json").write_text(json.dumps({"blocked": len(blocked), "counts": counts}))
'''
    result = subprocess.run([sys.executable, "-I", "-S", "-B", "-c", code, str(source), str(root)],
                            cwd=root, env=safe_environment(root), capture_output=True,
                            text=True, timeout=60, check=False)
    after_denied = {path: sha256(path.read_bytes()).hexdigest() if path.exists() else None
                    for path in denied_paths}
    assert after_denied == before_denied
    assert result.returncode == 0, result.stderr
    receipt = json.loads((root / "result.json").read_text())
    # These opens fail the scope check before the write-counter branch.
    assert receipt["blocked"] == 3 and receipt["counts"]["outside_write"] == 0


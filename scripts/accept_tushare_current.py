"""User-started two-stock data acceptance; never starts analysis or models.

Run with the project's Python and -I -S -B. --database is an explicit path
confirmation; it must name this checkout's existing data/market_history database.
An existing ledger, absent database, old schema or unmigrated request authority
stops before application imports and credential loading. No cleanup or retries.
"""
from __future__ import annotations

import argparse
import ast
import hashlib
import importlib.util
import json
import os
import sqlite3
import sys
from datetime import date
from pathlib import Path

SOURCE = Path(__file__).resolve().parents[1]
SDK_SHA256 = "42323c7e7acff1b0ffce0fe5232452b70cc276c17a3f449f7f43544fd82edd3f"


def load_support():
    path = SOURCE / "tradingagents/tushare_acceptance.py"
    spec = importlib.util.spec_from_file_location("_tushare_acceptance_support", path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def schema_version():
    tree = ast.parse((SOURCE / "tradingagents/market_history/schema.py").read_text(encoding="utf-8"))
    values = [node.value.value for node in tree.body
              if isinstance(node, ast.Assign) and isinstance(node.value, ast.Constant)
              and any(isinstance(target, ast.Name) and target.id == "SCHEMA_VERSION"
                      for target in node.targets)]
    if len(values) != 1 or type(values[0]) is not int:
        raise ValueError("schema version unavailable")
    return values[0]


def below(path, root):
    try:
        path.resolve().relative_to(root.resolve())
        return True
    except (OSError, ValueError):
        return False


def reject_alias(path):
    for component in (path, *path.parents):
        if component.exists() and (
            component.is_symlink()
            or getattr(component.lstat(), "st_file_attributes", 0) & 0x400
        ):
            raise ValueError("aliased acceptance path")


def discover_end(support, guard, runtime):
    """Two calendars under the same shared authority and acceptance send cap."""
    from datetime import timedelta
    from uuid import uuid4

    from tradingagents.dataflows import market_snapshot
    from tradingagents.market_history import coordinator
    from tradingagents.market_history.config import MarketHistoryConfig
    from tradingagents.market_history.store import MarketHistoryStore
    from tradingagents.picker.pit_config import PITConfig
    from tradingagents.picker.tushare_provider import TushareProvider

    config = MarketHistoryConfig.from_mapping(runtime)
    service, name = coordinator.upstream_service_identity_for_provider("tushare")
    with MarketHistoryStore.open_provider_request_authority(config) as store:
        owner = coordinator.ProviderRequestCoordinator(store)
        owner.register_upstream_service(service, name)
        owner.configure_operator_ceiling(upstream_service_id=service,
            minimum_interval=timedelta(seconds=1.5), allow_prewarming=False,
            configured_at=market_snapshot._coordinator_now())

        def workflow(execute_operation):
            provider = TushareProvider.create(PITConfig.from_env())

            def fetch(exchange, first, last):
                membership = {}

                def execute(endpoint, request):
                    def physical():
                        coordinator.record_active_physical_attempt_io()
                        frame = market_snapshot._typed_mainland_physical_request(request)
                        try:
                            if not frame.columns.is_unique:
                                raise ValueError
                            rows = frame.to_dict(orient="records")
                        except (AttributeError, TypeError, ValueError):
                            guard.stop("calendar_response_invalid")
                        support.validate_calendar_page(guard, exchange, rows, membership)
                        return frame
                    return execute_operation(endpoint, physical)

                return provider.fetch_range("trade_cal", exchange=exchange,
                    start_date=first.strftime("%Y%m%d"), end_date=last.strftime("%Y%m%d"),
                    request_executor=execute).to_dict(orient="records")

            return support.discover_session(guard, fetch).isoformat()

        # A new ledger is a new acceptance run. Never replay an earlier run's
        # completed workflow in place of this run's guarded calendar discovery.
        nonce = uuid4().hex
        result = owner.execute_physical_workflow(request_key="acceptance-calendar:" + nonce,
            upstream_service_id=service, owner_id="acceptance:" + nonce,
            priority=coordinator.RequestPriority.INTERACTIVE_MAINLAND,
            now=market_snapshot._coordinator_now, sleep=market_snapshot._coordinator_sleep,
            lease_duration=timedelta(minutes=2), max_physical_operations=support.LIMIT,
            wait_for_initial_pacing=True, cooldown_scope="market-snapshot", workflow=workflow)
        return result.value


def main(argv=None):
    if not (sys.flags.isolated and sys.flags.no_site and sys.dont_write_bytecode):
        raise SystemExit("Requires -I -S -B; no application was started.")
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--end", type=date.fromisoformat,
                        help="Optional common session within the prior 31 days; verified against both calendars")
    parser.add_argument("--database", required=True, type=Path)
    parser.add_argument("--ledger", required=True, type=Path,
                        help="New .jsonl file directly under this checkout's logs directory")
    options = parser.parse_args(argv)
    support = load_support()
    discovery = support.CalendarDiscoveryWindow.capture(options.end)
    data_root = SOURCE / "data/market_history"
    primary = data_root / "market_history.sqlite3"
    authority = primary.with_name(primary.name + ".provider-requests.sqlite3")
    ledger = options.ledger.absolute()
    reject_alias(data_root)
    reject_alias(ledger)
    if options.database.resolve() != primary.resolve():
        raise support.AcceptanceStop("database_confirmation_mismatch")
    if ledger.parent != SOURCE / "logs" or ledger.suffix != ".jsonl" or ledger.exists():
        raise support.AcceptanceStop("new_local_ledger_required")
    databases = {primary.resolve(), authority.resolve()}
    databases |= {path.with_name(path.name + ".payload-mutation.sqlite3") for path in tuple(databases)}
    version = schema_version()
    # No application import, dotenv or token access occurs during preflight.
    for path in (primary, authority):
        reject_alias(path)
        if not path.is_file():
            raise support.AcceptanceStop("existing_database_required")
        connection = sqlite3.connect(path.as_uri() + "?mode=ro", uri=True)
        try:
            support.validate_schema(connection, version)
            if path == authority:
                support.require_imported_authority(connection, primary)
        finally:
            connection.close()
    env_root = Path(sys.executable).absolute().parent.parent
    site = (env_root / "Lib/site-packages" if os.name == "nt" else
            env_root / "lib" / f"python{sys.version_info.major}.{sys.version_info.minor}" / "site-packages")
    sdk = site / "tushare/pro/client.py"
    if hashlib.sha256(sdk.read_bytes()).hexdigest() != SDK_SHA256:
        raise support.AcceptanceStop("sdk_source_pin_mismatch")
    print(f"Database={primary}\nPayloads={data_root / 'payloads'}\nSchema={version}\nLedger={ledger}")
    print(f"Stocks=600895.SH,000001.SZ Calendars={discovery.start}..{discovery.end} "
          "PhysicalLimit=24 RateLimit=40/min Models=0")
    ledger.parent.mkdir(exist_ok=True)
    with ledger.open("x", encoding="utf-8") as output:
        def record(value):
            output.write(json.dumps(value, ensure_ascii=False, sort_keys=True, allow_nan=False) + "\n")
            output.flush()
            os.fsync(output.fileno())

        guard = support.RequestGuard(discovery.start.isoformat(), discovery.end.isoformat(),
                                     record, discovery=discovery)

        def audit(event, args):
            guard.audit(event, args)
            if event == "open":
                path, mode, flags = args[:3]
                writing = (isinstance(mode, str) and any(char in mode for char in "wax+")) or (
                    isinstance(flags, int) and flags & (os.O_WRONLY | os.O_RDWR | os.O_CREAT | os.O_TRUNC))
                if writing and not isinstance(path, int):
                    value = Path(os.fsdecode(path)).absolute()
                    if value != ledger and not below(value, data_root):
                        guard.stop("outside_write")
            elif event == "sqlite3.connect":
                if Path(os.fsdecode(args[0])).resolve() not in databases:
                    guard.stop("outside_database")
            elif event in {"sqlite3.enable_load_extension", "sqlite3.load_extension"}:
                guard.stop("sqlite_extension_forbidden")
            elif event in {"os.remove", "os.rmdir", "os.mkdir", "os.chmod", "os.utime", "os.truncate"}:
                if not below(Path(os.fsdecode(args[0])), data_root):
                    guard.stop("outside_write")
            elif event in {"os.rename", "os.link", "os.symlink"}:
                if any(not below(Path(os.fsdecode(item)), data_root) for item in args[:2]):
                    guard.stop("outside_write")

        sys.addaudithook(audit)
        record({"kind": "started", "limit": 24, "symbols": list(support.SYMBOLS),
                "started_at": discovery.started_at.isoformat(), "run_date": discovery.run_date.isoformat(),
                "calendar_start": discovery.start.isoformat(), "calendar_end": discovery.end.isoformat(),
                "requested_end": options.end.isoformat() if options.end else None,
                "database": str(primary), "schema": version})
        connections = {}
        real_connect = sqlite3.connect

        class ExistingConnection(sqlite3.Connection):
            def __init__(self, *args, **kwargs):
                super().__init__(*args, **kwargs)
                connections[id(self)] = self
                self.set_authorizer(self.authorize)

            def close(self):
                connections.pop(id(self), None)
                super().close()

            def authorize(self, action, arg1, arg2, database, trigger):
                forbidden = {
                    sqlite3.SQLITE_ATTACH, sqlite3.SQLITE_DETACH, sqlite3.SQLITE_ALTER_TABLE,
                    sqlite3.SQLITE_CREATE_TABLE, sqlite3.SQLITE_CREATE_INDEX,
                    sqlite3.SQLITE_CREATE_TRIGGER, sqlite3.SQLITE_CREATE_VIEW,
                    sqlite3.SQLITE_DROP_TABLE, sqlite3.SQLITE_DROP_INDEX,
                    sqlite3.SQLITE_DROP_TRIGGER, sqlite3.SQLITE_DROP_VIEW,
                }
                if action in forbidden or (action == sqlite3.SQLITE_FUNCTION and arg2 == "load_extension"):
                    return sqlite3.SQLITE_DENY
                return sqlite3.SQLITE_OK

        def connect(database, *args, **kwargs):
            if len(args) > 4 or "factory" in kwargs:
                guard.stop("unexpected_database_factory")
            if Path(database).resolve() not in databases:
                guard.stop("outside_database")
            return real_connect(database, *args, factory=ExistingConnection, **kwargs)

        sqlite3.connect = sqlite3.dbapi2.connect = connect
        try:
            # Standard package initialization owns existing dotenv/credential
            # loading only after preflight and the network/write boundaries.
            os.chdir(SOURCE)
            os.environ["TRADINGAGENTS_PROJECT_ROOT"] = str(SOURCE)
            sys.path[:0] = [str(SOURCE), str(site)]
            import socket
            socket.has_ipv6 = False  # Avoid urllib3's optional local socket probe.
            from tradingagents import tushare_https
            from tradingagents.dataflows import config, market_snapshot
            from tradingagents.market_history import coordinator, current_cache
            from tradingagents.market_history.store import MarketHistoryStore

            def no_migration(store):
                support.validate_schema(store._connection, version)

            def no_import(store, source_path):
                if Path(source_path).resolve() != primary.resolve():
                    guard.stop("authority_source_mismatch")
                support.require_imported_authority(store._connection, primary)

            MarketHistoryStore._apply_migrations = no_migration
            MarketHistoryStore._import_legacy_provider_request_state = no_import
            if current_cache.compatibility_diagnostic() is not None:
                guard.stop("cache_source_binding_mismatch")
            runtime = config.get_config()
            if (runtime.get("market_history_mode") != "current"
                    or runtime.get("data_usage_mode") != "personal_research"
                    or runtime.get("mainland_capability_routing_mode") != "legacy"
                    or runtime.get("market_data_vendors", {}).get("cn_a", {}).get("core_stock_apis") != "tushare"
                    or Path(runtime["market_history_database_path"]).resolve() != primary.resolve()
                    or Path(runtime["market_history_payload_root"]).resolve() != (data_root / "payloads").resolve()):
                guard.stop("runtime_configuration_mismatch")
            # Explicit fixed caps; never alter the persistent local profile.
            config.set_config({"tushare_calls_per_minute": 40,
                               "tushare_operator_safety_ceiling_calls_per_minute": 40,
                               "market_history_current_force_refresh": False,
                               "market_history_current_repair_ranges": []})
            guard.bind(tushare_https, coordinator)
            def require_closed():
                if connections:
                    guard.stop("unclosed_database_connections")

            selected_end = discover_end(support, guard, config.get_config())
            require_closed()
            print(f"ScheduledEnd={selected_end} Window={guard.start}..{guard.end}; checking price coverage next.")
            support.run_data_phases(guard, market_snapshot, record, end=selected_end,
                                    require_closed=require_closed)
            print(f"PASS: {guard.attempts}/24 physical requests; reopened reuse=0; model calls=0.")
            return 0
        except BaseException as exc:
            code = str(exc) if isinstance(exc, support.AcceptanceStop) else "data_acceptance_failed"
            record({"kind": "stopped", "code": code, "physical_attempts": guard.attempts})
            print(f"STOP: {code}. Keep ledger and partial data; do not retry this run.", file=sys.stderr)
            return 1
        finally:
            for connection in tuple(connections.values()):
                connection.close()


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except SystemExit:
        raise
    except BaseException:
        raise SystemExit("STOP: acceptance preflight failed; no automatic retry.") from None

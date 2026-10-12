"""Synthetic acceptance-budget contracts; no credential or live transport access."""
from contextlib import contextmanager
from datetime import date, datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

from tradingagents.tushare_acceptance import (
    BASE,
    FIELDS,
    AcceptanceStop,
    RequestGuard,
    closed_date,
    five_year_start,
    require_imported_authority,
    run_data_phases,
    validate_schema,
)


def fixture_guard():
    records, sent, ticks = [], [], [0.0]
    guard = RequestGuard("2021-10-08", "2026-10-08", records.append,
                         now=lambda: ticks[0], sleep=lambda seconds: ticks.__setitem__(0, ticks[0] + seconds))

    def post(url, **kwargs):
        sent.append((url, ticks[0]))
        return SimpleNamespace(text='{"code":0,"data":{"fields":[],"items":[]}}')

    guard.transport = SimpleNamespace(post=post)
    return guard, records, sent, ticks


def send(guard, *, endpoint="daily", params=None, symbol="600895.SH", **changes):
    params = params if params is not None else {
        "ts_code": symbol, "start_date": "20211008", "end_date": "20261008",
        "offset": 0, "limit": 5000, "ts_type_name": BASE,
    }
    payload = {"api_name": endpoint, "token": "SYNTHETIC_NOT_A_CREDENTIAL",
               "params": params, "fields": FIELDS.get(endpoint, "invalid")}
    payload.update(changes)
    return guard.coordinated(lambda owner, **kw: kw["physical_request"](), object(),
        expected_service="test-service", upstream_service_id="test-service", operation=endpoint,
        physical_request=lambda: guard.post(BASE + "/" + endpoint, json=payload))


def test_limit_shared_across_stocks_and_failure_latches():
    guard, records, sent, _ = fixture_guard()
    for index in range(24):
        send(guard, symbol="600895.SH" if index % 2 else "000001.SZ")
    with pytest.raises(AcceptanceStop, match="physical_budget_exhausted"):
        send(guard)
    with pytest.raises(AcceptanceStop):
        send(guard, symbol="000001.SZ")
    assert guard.attempts == len(sent) == len(records) == 24
    assert all(b[1] - a[1] >= 1.5 for a, b in zip(sent, sent[1:], strict=False))
    assert all("token" not in row and "params" not in row for row in records)


@pytest.mark.parametrize("failure", ["transport", "ledger"])
def test_failed_attempt_never_refunded_or_retried(failure):
    guard, _, sent, _ = fixture_guard()

    def fail(*args, **kwargs):
        raise ValueError("SYNTHETIC_PRIVATE_VALUE_must_not_escape")

    if failure == "transport":
        guard.transport.post = fail
    else:
        guard.record = fail
    with pytest.raises(AcceptanceStop, match="transport_or_ledger_failure"):
        send(guard)
    with pytest.raises(AcceptanceStop):
        send(guard)
    assert guard.attempts == 1
    assert sent == []


def test_reuse_miss_stops_before_sender():
    guard, records, sent, _ = fixture_guard()
    with guard.cache_only(), pytest.raises(AcceptanceStop, match="reuse_network_forbidden"):
        send(guard)
    assert guard.attempts == 0 and not records and not sent
    with pytest.raises(AcceptanceStop):
        send(guard)


@pytest.mark.parametrize("change", [
    {"ts_code": "600519.SH"}, {"start_date": "20200101"}, {"end_date": "20261009"},
    {"limit": True}, {"limit": 5001}, {"offset": -1}, {"offset": "0"},
    {"ts_type_name": "http://api.waditu.com/dataapi"}, {"unexpected": "value"},
])
def test_scope_rejected_before_budget_reservation(change):
    guard, records, sent, _ = fixture_guard()
    params = {"ts_code": "600895.SH", "start_date": "20211008", "end_date": "20261008",
              "offset": 0, "limit": 5000, "ts_type_name": BASE, **change}
    with pytest.raises(AcceptanceStop):
        send(guard, params=params)
    assert guard.attempts == 0 and not records and not sent


def test_all_factor_pages_and_calendar_are_counted():
    guard, records, sent, _ = fixture_guard()
    for offset in (0, 5000):
        send(guard, endpoint="adj_factor", params={"ts_code": "600895.SH", "end_date": "20261008",
             "offset": offset, "limit": 5000, "ts_type_name": BASE})
    for exchange in ("SSE", "SZSE"):
        send(guard, endpoint="trade_cal", params={"exchange": exchange, "start_date": "20211008",
             "end_date": "20261008", "offset": 0, "limit": 5000, "ts_type_name": BASE})
    assert len(sent) == len(records) == guard.attempts == 4


@pytest.mark.parametrize("swallow", [False, True])
def test_original_coordinator_cleanup_cannot_swallow_stop(swallow):
    guard, _, sent, _ = fixture_guard()

    def coordinator(owner, **kwargs):
        try:
            kwargs["physical_request"]()
        except BaseException:
            if not swallow:
                raise RuntimeError("translated") from None
        return "swallowed"

    with pytest.raises(AcceptanceStop, match="unexpected_network"):
        guard.coordinated(coordinator, object(), expected_service="test-service",
            upstream_service_id="test-service", operation="daily",
            physical_request=lambda: guard.audit("socket.getaddrinfo", ()))
    assert not sent and guard.attempts == 0


def test_schema_and_legacy_import_must_already_exist(tmp_path):
    import sqlite3

    connection = sqlite3.connect(tmp_path / "existing.sqlite3")
    try:
        with pytest.raises(AcceptanceStop, match="existing_schema_required"):
            validate_schema(connection, 2)
        connection.execute("CREATE TABLE schema_migrations (version INTEGER)")
        connection.execute("INSERT INTO schema_migrations VALUES (1)")
        with pytest.raises(AcceptanceStop, match="schema_migration_required"):
            validate_schema(connection, 2)
        connection.execute("INSERT INTO schema_migrations VALUES (2)")
        validate_schema(connection, 2)
        connection.execute("CREATE TABLE provider_request_state_imports (source_database_path TEXT)")
        with pytest.raises(AcceptanceStop, match="coordinator_state_import_required"):
            require_imported_authority(connection, tmp_path / "main.sqlite3")
        assert connection.execute("SELECT COUNT(*) FROM provider_request_state_imports").fetchone()[0] == 0
    finally:
        connection.close()


def test_reopened_request_contexts_preserve_evidence_without_sends():
    guard, records, sent, _ = fixture_guard()
    contexts, closed = [], []

    @contextmanager
    def new_context():
        marker = object()
        contexts.append(marker)
        yield

    def snapshot(symbol, start, end, **kwargs):
        return SimpleNamespace(symbol=symbol, provider="tushare", frame_sha256="synthetic-frame",
            snapshot_id=symbol, retrieved_at="2026-10-09T00:00:00Z", adjustment_basis="qfq",
            effective_trading_date=end, pin_membership_digest="synthetic-pin", frame=range(260),
            history_store_status="current_only_cached")

    module = SimpleNamespace(authoritative_snapshot_run=new_context,
                             get_authoritative_market_snapshot=snapshot)
    run_data_phases(guard, module, records.append, end="2026-10-08",
                    require_closed=lambda: closed.append(True))
    assert len(contexts) == len(set(contexts)) == len(closed) == 4
    assert not sent and records[-1] == {"kind": "completed", "physical_attempts": 0,
                                       "reuse_attempts": 0, "model_calls": 0}


def test_date_requires_previous_day_and_leap_start():
    assert five_year_start(date(2024, 2, 29)) == date(2019, 2, 28)
    with pytest.raises(AcceptanceStop, match="previous_completed"):
        closed_date(date(2026, 10, 12), datetime(2026, 10, 12, 13, tzinfo=timezone.utc))
    closed_date(date(2026, 10, 9), datetime(2026, 10, 12, 13, tzinfo=timezone.utc))


@pytest.mark.parametrize("workflow", [False, True])
@pytest.mark.parametrize("retry_after", ["success", None, 23.0])
def test_bound_real_coordinator_counts_sends_and_persists_429(tmp_path, monkeypatch, workflow, retry_after):
    import json

    import pandas as pd

    from tradingagents import tushare_https as tls
    from tradingagents.dataflows.market_snapshot import _typed_mainland_physical_request
    from tradingagents.market_history import coordinator as module
    from tradingagents.market_history.config import (
        DataUsageMode,
        MarketHistoryConfig,
        MarketHistoryMode,
    )
    from tradingagents.market_history.store import MarketHistoryStore
    from tradingagents.picker.tushare_provider import TushareProvider

    guard, records, sent, ticks = fixture_guard()
    original_transport = guard.transport

    def transport(url, **kwargs):
        if retry_after != "success":
            sent.append((url, ticks[0]))
            raise tls.TushareHTTPSFailure("rate_limited", 429, retry_after)
        return original_transport.post(url, **kwargs)

    # Save all patched bindings for fixture teardown. Only the final sender is
    # synthetic; SDK wrapper/provider translation and SQLite coordinator are real.
    monkeypatch.setattr(tls, "HTTPSPost", lambda: SimpleNamespace(post=transport))
    cls = module.ProviderRequestCoordinator
    monkeypatch.setattr(cls, "execute_direct_physical_request", cls.execute_direct_physical_request)
    monkeypatch.setattr(cls, "execute_physical_workflow", cls.execute_physical_workflow)
    guard.bind(tls, module)
    def sdk_query(endpoint, fields="", **params):
        response = tls.HTTPSPost().post(BASE + "/" + endpoint,
            json={"api_name": endpoint, "token": "SYNTHETIC_NOT_A_CREDENTIAL",
                  "fields": fields, "params": params})
        data = json.loads(response.text)["data"]
        return pd.DataFrame(data["items"], columns=data["fields"])

    client = tls._Client(sdk_query)
    provider = TushareProvider(client)
    config = MarketHistoryConfig(MarketHistoryMode.CURRENT, tmp_path / "db.sqlite3",
        tmp_path / "payloads", tmp_path / "backups", DataUsageMode.PERSONAL_RESEARCH)
    clock = datetime(2026, 10, 9, 10, tzinfo=timezone.utc)
    service, name = module.upstream_service_identity_for_provider("tushare")

    def physical():
        if workflow:
            module.record_active_physical_attempt_io()
        return _typed_mainland_physical_request(lambda: provider._request("daily", fields=FIELDS["daily"],
            ts_code="600895.SH", start_date="20211008", end_date="20261008",
            limit=5000, offset=0, ts_type_name=BASE))

    with MarketHistoryStore.open(config) as store:
        coordinator = cls(store)
        coordinator.register_upstream_service(service, name)
        coordinator.configure_operator_ceiling(upstream_service_id=service,
            minimum_interval=timedelta(seconds=1.5), allow_prewarming=False, configured_at=clock)
        common = {"request_key": "synthetic-acceptance", "upstream_service_id": service,
            "owner_id": "synthetic-owner", "priority": module.RequestPriority.INTERACTIVE_MAINLAND,
            "now": lambda: clock + timedelta(seconds=ticks[0]), "sleep": guard.sleep,
            "lease_duration": timedelta(minutes=2)}

        def run():
            if workflow:
                return coordinator.execute_physical_workflow(**common, max_physical_operations=24,
                    workflow=lambda execute: execute("daily", physical))
            return coordinator.execute_direct_physical_request(**common, operation="daily", physical_request=physical)

        if retry_after == "success":
            assert run().physical_attempt_count == 1
            with guard.cache_only(), pytest.raises(AcceptanceStop, match="reuse_network_forbidden"):
                run()
        else:
            with pytest.raises(AcceptanceStop, match="transport_or_ledger_failure"):
                run()
            with pytest.raises(AcceptanceStop):
                run()
        assert guard.attempts == len(records) == len(sent) == 1
    # Closing and reopening must preserve the actual coordinator evidence.
    with MarketHistoryStore.open(config) as reopened:
        row = reopened._connection.execute(
            "SELECT outcome, cooldown_changed, retry_after_seconds FROM provider_request_attempts").fetchone()
        if retry_after == "success":
            assert tuple(row) == ("available", 0, None)
        else:
            assert tuple(row) == ("rate_limited", 1, 300.0 if retry_after is None else retry_after)
            assert reopened._connection.execute("SELECT COUNT(*) FROM request_cooldowns").fetchone()[0] == 1


def test_workflow_tickets_are_single_use_and_inside_permitted_callback():
    guard, _, sent, _ = fixture_guard()
    seen = []

    def coordinator(owner, **kwargs):
        def operation(endpoint, callback):
            assert getattr(guard.local, "ticket", None) is None
            seen.append(endpoint)
            callback()
            callback()
        return kwargs["workflow"](operation)

    payload = {"api_name": "daily", "token": "SYNTHETIC_NOT_A_CREDENTIAL", "fields": FIELDS["daily"],
        "params": {"ts_code": "600895.SH", "start_date": "20211008", "end_date": "20261008",
                   "limit": 5000, "offset": 0, "ts_type_name": BASE}}
    with pytest.raises(AcceptanceStop, match="retry_or_nested_request"):
        guard.coordinated_workflow(coordinator, object(), expected_service="test-service",
            upstream_service_id="test-service", workflow=lambda execute:
                execute("daily", lambda: guard.post(BASE + "/daily", json=payload)))
    assert guard.attempts == len(sent) == len(seen) == 1


@pytest.mark.parametrize("case,code", [
    ("path", "database_confirmation_mismatch"),
    ("ledger", "new_local_ledger_required"),
    ("missing", "existing_database_required"),
    ("schema", "schema_migration_required"),
    ("authority", "coordinator_state_import_required"),
])
def test_launcher_preflight_stops_before_application_loading(tmp_path, monkeypatch, case, code):
    import importlib.util
    import sqlite3
    from pathlib import Path

    import tradingagents.tushare_acceptance as support

    path = Path(__file__).resolve().parents[1] / "scripts/accept_tushare_current.py"
    spec = importlib.util.spec_from_file_location("synthetic_acceptance_launcher", path)
    launcher = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(launcher)
    monkeypatch.setattr(launcher, "SOURCE", tmp_path)
    monkeypatch.setattr(launcher, "load_support", lambda: support)
    monkeypatch.setattr(launcher, "schema_version", lambda: 1)
    # Replace only this module's binding, never the interpreter's actual flags.
    # It intentionally has no executable/path: passing preflight cannot launch.
    monkeypatch.setattr(launcher, "sys", SimpleNamespace(
        flags=SimpleNamespace(isolated=True, no_site=True), dont_write_bytecode=True))
    primary = tmp_path / "data/market_history/market_history.sqlite3"
    primary.parent.mkdir(parents=True)
    ledger = tmp_path / "logs/acceptance.jsonl"
    ledger.parent.mkdir()
    if case == "ledger":
        ledger.write_text("existing evidence", encoding="utf-8")
    if case in {"schema", "authority"}:
        for database in (primary, primary.with_name(primary.name + ".provider-requests.sqlite3")):
            connection = sqlite3.connect(database)
            try:
                connection.execute("CREATE TABLE schema_migrations(version INTEGER)")
                if case == "authority":
                    connection.execute("INSERT INTO schema_migrations VALUES(1)")
                    connection.execute("CREATE TABLE provider_request_state_imports(source_database_path TEXT)")
                connection.commit()
            finally:
                connection.close()
    original_connect = sqlite3.connect
    exact_uris = {database.as_uri() + "?mode=ro": database for database in (
        primary, primary.with_name(primary.name + ".provider-requests.sqlite3"))}

    def synthetic_read_only(database, *args, **kwargs):
        # The outer offline boundary continues to reject every real URI. This
        # test seam accepts just the two existing synthetic fixtures and makes
        # their already-guarded connection query-only before launcher code runs.
        assert not args and kwargs == {"uri": True} and database in exact_uris
        connection = original_connect(exact_uris[database])
        connection.execute("PRAGMA query_only=ON")
        return connection

    monkeypatch.setattr(launcher.sqlite3, "connect", synthetic_read_only)
    selected = primary.with_name("wrong.sqlite3") if case == "path" else primary
    with pytest.raises(AcceptanceStop, match=code):
        launcher.main(["--end", "2026-10-08", "--database", str(selected), "--ledger", str(ledger)])
    assert ledger.exists() is (case == "ledger")
    if case == "ledger":
        assert ledger.read_text(encoding="utf-8") == "existing evidence"

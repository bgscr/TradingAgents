"""Synthetic acceptance-budget contracts; no credential or live transport access."""
from contextlib import contextmanager
from datetime import date, datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

from tradingagents.tushare_acceptance import (
    BASE,
    FIELDS,
    AcceptanceStop,
    CalendarDiscoveryWindow,
    RequestGuard,
    closed_date,
    discover_session,
    five_year_start,
    local_platform_metadata,
    require_imported_authority,
    run_data_phases,
    validate_calendar_page,
    validate_schema,
)


@pytest.mark.parametrize("tag,machine", [
    ("win32", "x86"), ("win-amd64", "AMD64"), ("win-arm32", "ARM"), ("win-arm64", "ARM64"),
])
@pytest.mark.parametrize("fail", [False, True])
def test_windows_metadata_is_process_scoped_and_restored(monkeypatch, tag, machine, fail):
    import platform
    import socket
    import sysconfig

    import tradingagents.tushare_acceptance as support

    monkeypatch.setattr(support, "sys", SimpleNamespace(platform="win32", getwindowsversion=
                        lambda: SimpleNamespace(major=6, minor=2, build=9200,
                                                platform_version=(10, 0, 12345))))
    monkeypatch.setattr(sysconfig, "get_platform", lambda: tag)
    before = platform.uname, platform.win32_ver, platform._platform_cache, socket.has_ipv6
    class DeliberateStop(BaseException):
        pass
    try:
        with local_platform_metadata():
            assert platform.uname() == ("Windows", "", "", "10.0.12345", machine, "")
            assert platform.uname().processor == ""
            assert platform.win32_ver("ignored", "ignored", "ignored", "ignored") == (
                "", "10.0.12345", "", "")
            assert socket.has_ipv6 is False
            platform._platform_cache[(False, False)] = "scoped value"
            if fail:
                raise DeliberateStop
    except DeliberateStop:
        pass
    assert platform.uname is before[0] and platform.win32_ver is before[1]
    assert platform._platform_cache is before[2] and socket.has_ipv6 is before[3]


def test_unknown_windows_process_platform_stops_and_restores(monkeypatch):
    import platform
    import socket
    import sysconfig

    import tradingagents.tushare_acceptance as support

    monkeypatch.setattr(support, "sys", SimpleNamespace(platform="win32"))
    monkeypatch.setattr(sysconfig, "get_platform", lambda: "unrecognized")
    before = platform.uname, platform.win32_ver, platform._platform_cache, socket.has_ipv6
    with pytest.raises(AcceptanceStop, match="unsupported_process_platform"), local_platform_metadata():
        pytest.fail("unknown architecture must not enter the data runtime")
    assert (platform.uname, platform.win32_ver, platform._platform_cache, socket.has_ipv6) == before


@pytest.mark.parametrize("event,stage,expected", [
    ("socket.gethostname", "application_imports", "socket.gethostname"),
    ("socket.getaddrinfo", "calendar_discovery", "socket.getaddrinfo"),
    ("subprocess.Popen", "data_phases", "subprocess.Popen"),
    ("socket.PRIVATE_VALUE", "PRIVATE_VALUE", "other_audit_event"),
])
def test_audit_rejection_projects_only_safe_event_and_stage(event, stage, expected):
    guard, records, sent, _ = fixture_guard()
    guard.stage = stage
    with pytest.raises(AcceptanceStop):
        guard.audit(event, ("PRIVATE_VALUE", {"token": "PRIVATE_VALUE"}))
    assert guard.rejection == {"audit_event": expected,
                               "stage": stage if stage != "PRIVATE_VALUE" else "unknown"}
    assert "PRIVATE_VALUE" not in repr(guard.rejection)
    with pytest.raises(AcceptanceStop):
        guard.audit("socket.bind", ())
    assert guard.rejection["audit_event"] == expected
    assert guard.attempts == 0 and not records and not sent


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


@pytest.mark.parametrize("phase,status", [
    (phase, status) for phase in ("initial", "reopened")
    for status in ("degraded", "live", "refreshed", None)
] + [("reopened", "current_only_refreshed")])
def test_acceptance_rejects_unproven_persistence(phase, status):
    guard, records, sent, _ = fixture_guard()
    calls = []

    @contextmanager
    def context():
        yield

    def snapshot(symbol, start, end, **kwargs):
        calls.append(symbol)
        current_phase = "initial" if len(calls) <= 2 else "reopened"
        return SimpleNamespace(symbol=symbol, provider="tushare", frame_sha256="synthetic-frame",
            snapshot_id=symbol, retrieved_at="2026-10-09T00:00:00Z", adjustment_basis="qfq",
            effective_trading_date=end, pin_membership_digest="synthetic-pin", frame=range(260),
            history_store_status=status if current_phase == phase else "current_only_cached")

    module = SimpleNamespace(authoritative_snapshot_run=context,
                             get_authoritative_market_snapshot=snapshot)
    with pytest.raises(AcceptanceStop, match="persistence_not_confirmed"):
        run_data_phases(guard, module, records.append, end="2026-10-08")
    assert not any(row["kind"] == "completed" for row in records)
    assert records[-1]["kind"] == phase and records[-1]["history_store_status"] == status
    assert len(calls) == (1 if phase == "initial" else 3)
    assert not sent and guard.attempts == 0


def test_date_requires_previous_day_and_leap_start():
    assert five_year_start(date(2024, 2, 29)) == date(2019, 2, 28)
    with pytest.raises(AcceptanceStop, match="previous_completed"):
        closed_date(date(2026, 10, 12), datetime(2026, 10, 12, 13, tzinfo=timezone.utc))
    closed_date(date(2026, 10, 9), datetime(2026, 10, 12, 13, tzinfo=timezone.utc))


def calendar_case(requested_end=None):
    window = CalendarDiscoveryWindow.capture(requested_end,
        started_at=datetime(2026, 10, 12, 15, 59, tzinfo=timezone.utc))
    records, sent, ticks, calls = [], [], [0.0], []
    guard = RequestGuard(window.start.isoformat(), window.end.isoformat(), records.append,
        discovery=window, now=lambda: ticks[0], sleep=lambda seconds: ticks.__setitem__(0, ticks[0] + seconds))
    guard.transport = SimpleNamespace(post=lambda *args, **kwargs: sent.append(ticks[0]))
    rows = {exchange: [{"exchange": exchange, "cal_date": (window.start + timedelta(days=i)).strftime("%Y%m%d"),
                       "is_open": int((window.start + timedelta(days=i)).weekday() < 5)} for i in range(31)]
            for exchange in ("SSE", "SZSE")}

    def fetch(exchange, first, last):
        calls.append((exchange, first, last))
        send(guard, endpoint="trade_cal", params={"exchange": exchange,
            "start_date": first.strftime("%Y%m%d"), "end_date": last.strftime("%Y%m%d"),
            "offset": 0, "limit": 5000, "ts_type_name": BASE})
        return rows[exchange]

    return SimpleNamespace(guard=guard, records=records, sent=sent, rows=rows,
                           window=window, fetch=fetch, calls=calls, ticks=ticks)


@pytest.mark.parametrize("explicit", [None, date(2026, 10, 8)])
def test_discovery_checks_both_full_calendars_before_selecting_end(explicit):
    rig = calendar_case(explicit)
    selected = discover_session(rig.guard, rig.fetch)
    assert selected == (explicit or date(2026, 10, 9))
    assert rig.guard.start == five_year_start(selected) and rig.guard.end == selected
    assert rig.guard.attempts == len(rig.sent) == 2 and rig.sent == [0.0, 1.5]
    assert rig.calls == [(exchange, date(2026, 9, 11), date(2026, 10, 11)) for exchange in ("SSE", "SZSE")]
    proof = rig.records[-1]
    assert proof["kind"] == "calendar_selected" and proof["publication_checked"] is False
    assert all(len(days) == 31 for days in proof["calendars"].values())
    assert not rig.guard.discovering


@pytest.mark.parametrize("fault,code,count", [
    ("empty", "calendar_coverage_incomplete", 1),
    ("missing", "calendar_coverage_incomplete", 1),
    ("duplicate", "calendar_response_invalid", 1),
    ("boolean", "calendar_response_invalid", 1),
    ("exchange", "calendar_response_invalid", 1),
    ("out_of_range", "calendar_response_invalid", 1),
    ("disagree", "calendar_exchange_disagreement", 2),
    ("all_closed", "calendar_no_common_open_session", 2),
])
def test_discovery_fails_closed_for_invalid_or_conflicting_calendars(fault, code, count):
    rig = calendar_case()
    if fault == "empty":
        rig.rows["SSE"].clear()
    elif fault == "missing":
        rig.rows["SSE"].pop()
    elif fault == "duplicate":
        rig.rows["SSE"].append(dict(rig.rows["SSE"][0]))
    elif fault == "boolean":
        rig.rows["SSE"][0]["is_open"] = True
    elif fault == "exchange":
        rig.rows["SSE"][0]["exchange"] = "SZSE"
    elif fault == "out_of_range":
        rig.rows["SSE"][0]["cal_date"] = "20261012"
    elif fault == "disagree":
        rig.rows["SZSE"][0]["is_open"] = 1 - rig.rows["SZSE"][0]["is_open"]
    else:
        for rows in rig.rows.values():
            for row in rows:
                row["is_open"] = 0
    with pytest.raises(AcceptanceStop, match=code):
        discover_session(rig.guard, rig.fetch)
    assert rig.guard.attempts == len(rig.sent) == count
    assert all(item["kind"] == "physical_attempt" for item in rig.records)
    with pytest.raises(AcceptanceStop, match=code):
        rig.guard.ready()


def test_raw_calendar_page_validation_retains_dates_across_pages():
    rig = calendar_case()
    membership = {}
    validate_calendar_page(rig.guard, "SSE", rig.rows["SSE"][:20], membership)
    with pytest.raises(AcceptanceStop, match="calendar_response_invalid"):
        validate_calendar_page(rig.guard, "SSE", rig.rows["SSE"][19:], membership)
    assert rig.guard.discovering and rig.guard.stopped == "calendar_response_invalid"


def test_explicit_closed_day_is_rejected_after_both_calendar_queries():
    rig = calendar_case(date(2026, 10, 10))
    with pytest.raises(AcceptanceStop, match="explicit_end_not_common"):
        discover_session(rig.guard, rig.fetch)
    assert rig.guard.attempts == len(rig.sent) == 2


@pytest.mark.parametrize("end", [date(2026, 9, 10), date(2026, 10, 12)])
def test_explicit_date_cannot_expand_discovery_scope(end):
    with pytest.raises(AcceptanceStop, match="outside_recent_calendar_window"):
        calendar_case(end)


def test_discovery_window_is_frozen_across_shanghai_midnight(monkeypatch):
    import tradingagents.tushare_acceptance as support
    rig = calendar_case()
    clock = [rig.window.started_at]
    monkeypatch.setattr(support, "utc_now", lambda: clock[0])
    def fetch(exchange, first, last):
        rows = rig.fetch(exchange, first, last)
        clock[0] += timedelta(minutes=2)
        return rows
    selected = discover_session(rig.guard, fetch)
    assert clock[0].astimezone(timezone(timedelta(hours=8))).date() == date(2026, 10, 13)
    assert selected == date(2026, 10, 9)
    assert all(first == date(2026, 9, 11) and last == date(2026, 10, 11) for _, first, last in rig.calls)


def test_discovery_pages_can_exhaust_shared_budget_before_second_exchange():
    rig = calendar_case()
    def pages(exchange, first, last):
        if exchange == "SZSE":
            return rig.fetch(exchange, first, last)
        for page in range(24):
            send(rig.guard, endpoint="trade_cal", params={"exchange": exchange,
                "start_date": first.strftime("%Y%m%d"), "end_date": last.strftime("%Y%m%d"),
                "offset": page * 5000, "limit": 5000, "ts_type_name": BASE})
        return rig.rows[exchange]
    with pytest.raises(AcceptanceStop, match="physical_budget_exhausted"):
        discover_session(rig.guard, pages)
    assert rig.guard.attempts == len(rig.sent) == 24
    assert not any(item["kind"] == "calendar_selected" for item in rig.records)


def test_calendar_attempts_reduce_later_price_budget_without_reset():
    rig = calendar_case()
    discover_session(rig.guard, rig.fetch)
    for index in range(22):
        send(rig.guard, params={"ts_code": "600895.SH" if index % 2 else "000001.SZ",
            "start_date": rig.guard.start.strftime("%Y%m%d"), "end_date": rig.guard.end.strftime("%Y%m%d"),
            "offset": index * 5000, "limit": 5000, "ts_type_name": BASE})
    with pytest.raises(AcceptanceStop, match="physical_budget_exhausted"):
        send(rig.guard)
    assert rig.guard.attempts == len(rig.sent) == 24
    assert all(b - a >= 1.5 for a, b in zip(rig.sent, rig.sent[1:], strict=False))


@pytest.mark.parametrize("operation", ["data", "send", "range"])
def test_discovery_cannot_send_prices_or_run_data_before_selection(operation):
    rig = calendar_case()
    with pytest.raises(AcceptanceStop):
        if operation == "data":
            run_data_phases(rig.guard, SimpleNamespace(), rig.records.append, end=rig.guard.end.isoformat())
        elif operation == "send":
            send(rig.guard)
        else:
            send(rig.guard, endpoint="trade_cal", params={"exchange": "SSE", "start_date": "20260912",
                "end_date": "20261011", "offset": 0, "limit": 5000, "ts_type_name": BASE})
    assert rig.guard.attempts == len(rig.sent) == 0


def test_selection_ledger_failure_latches_before_data_phase():
    rig = calendar_case()
    def record(item):
        if item["kind"] == "calendar_selected":
            raise OSError("synthetic ledger failure")
        rig.records.append(item)
    rig.guard.record = record
    with pytest.raises(AcceptanceStop, match="calendar_ledger_failure"):
        discover_session(rig.guard, rig.fetch)
    assert rig.guard.attempts == len(rig.sent) == 2 and rig.guard.discovering


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
@pytest.mark.parametrize("explicit_end", [False, True])
def test_launcher_preflight_stops_before_application_loading(tmp_path, monkeypatch, case, code, explicit_end):
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
    monkeypatch.setattr(support, "utc_now", lambda: datetime(2026, 10, 9, 10, tzinfo=timezone.utc))
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
        launcher.main((["--end", "2026-10-08"] if explicit_end else []) +
                      ["--database", str(selected), "--ledger", str(ledger)])
    assert ledger.exists() is (case == "ledger")
    if case == "ledger":
        assert ledger.read_text(encoding="utf-8") == "existing evidence"

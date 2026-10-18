"""Synthetic CURRENT SSE preparation through real publication, reader and cache."""
import copy
from dataclasses import replace
from datetime import date, datetime, timedelta, timezone

import pandas as pd
import pytest

# This fixture requires the actual twelve-case matrix to have passed first.
from tests.test_market_history_persistent_reuse import validated_manifest  # noqa: F401
from tradingagents import budget_readiness
from tradingagents.dataflows import config as config_module, market_snapshot
from tradingagents.dataflows.tushare_market_history import load_calendar
from tradingagents.default_config import DEFAULT_CONFIG
from tradingagents.market_history import current_cache as cache
from tradingagents.market_history.calendar import MarketSessionCalendarIntegrityError
from tradingagents.market_history.config import MarketHistoryConfig, MarketHistoryMode
from tradingagents.market_history.store import MarketHistoryStore
from tradingagents.picker.tushare_provider import TushareProvider

START, END = date(2026, 9, 1), date(2026, 10, 8)
OBSERVED = datetime(2026, 10, 9, 10, tzinfo=timezone.utc)
SYMBOL = "601328.SS"


def rows(start=START, end=END, exchange="SSE"):
    return [{"exchange": exchange, "cal_date": day.strftime("%Y%m%d"),
             "is_open": str(int(day.weekday() < 5))}
            for day in pd.date_range(start, end)]


def publication(data=None, *, start=START, end=END, page_size=5000, symbol=SYMBOL):
    values = rows(start, end) if data is None else data
    calls = []

    class Client:
        def query(self, endpoint, **kwargs):
            assert endpoint == "trade_cal"
            calls.append(kwargs)
            return pd.DataFrame(values[kwargs["offset"]:kwargs["offset"] + kwargs["limit"]])

    attempts = []

    def execute(endpoint, request):
        attempts.append(endpoint)
        return request()

    result = load_calendar(symbol, str(start), str(end),
        provider=TushareProvider(Client(), page_size), request_executor=execute)
    assert len(calls) == len(attempts)
    return replace(result, observed_at=OBSERVED), calls


@pytest.fixture
def settings(tmp_path, monkeypatch):
    config = copy.deepcopy(DEFAULT_CONFIG)
    config.update(market_history_mode="current", data_usage_mode="personal_research",
        market_history_database_path=str(tmp_path / "history.sqlite3"),
        market_history_payload_root=str(tmp_path / "payloads"),
        market_history_backup_root=str(tmp_path / "backups"),
        market_data_vendors={"cn_a": {"core_stock_apis": "akshare"}},
        mainland_capability_routing_mode="legacy")
    monkeypatch.setattr(config_module, "_config", config)
    return config, MarketHistoryConfig.from_mapping(config)


def persist(settings, item=None):
    item = publication()[0] if item is None else item
    with MarketHistoryStore.open(settings[1]) as store:
        result = store.publish_session_calendar(item)
    return item, result


def test_publish_reopen_exact_source_and_actual_calendar_reader(settings, monkeypatch):
    item, result = persist(settings)
    with MarketHistoryStore.open(settings[1]) as store:
        for symbol in ("601328.SH", "601328.SS"):
            restored = cache.read_prepared_sse_calendar(store, symbol, START, END)
            assert restored.raw_payload == item.raw_payload
            assert restored.sessions == item.sessions
            assert restored.observed_at == item.observed_at
            assert restored.provider == item.provider
            assert restored.calendar_revision_id == result.calendar_revision_id
            assert restored.payload_contract == cache.SSE_CALENDAR_PROOF
            assert not restored.provider.strict_history_qualified
        # Generic exact CAL-02 reader must not admit this different source.
        with pytest.raises(MarketSessionCalendarIntegrityError):
            store.read_session_calendar_revision(result.calendar_revision_id)

    def forbidden(*args, **kwargs):
        pytest.fail("prepared calendar must not acquire or open provider authority")

    monkeypatch.setattr(MarketHistoryStore, "open_provider_request_authority", forbidden)
    restored = market_snapshot._ensure_analysis_calendar(str(START), str(END), symbol=SYMBOL)
    assert restored.calendar_revision_id == result.calendar_revision_id
    issues, checked = [], []
    budget_readiness._inspect_market(settings[0], SYMBOL, START, END, issues, checked)
    assert not issues
    assert any(x.location == "calendar" and x.status == "verified_full_natural_date_window"
               and x.requires_provider_request is False for x in checked)


@pytest.mark.parametrize("defect", ["missing", "duplicate", "duplicate_cross_page", "outside", "exchange", "bad_date"])
def test_invalid_actual_response_rejected_before_publication(defect):
    data = rows()
    if defect == "missing":
        data.pop(4)
    elif defect == "duplicate":
        data.insert(1, data[0].copy())
    elif defect == "duplicate_cross_page":
        data.insert(5, data[4].copy())
    elif defect == "outside":
        data[0]["cal_date"] = "20260831"
    elif defect == "exchange":
        data[0]["exchange"] = "SZSE"
    else:
        data[0]["cal_date"] = "202691"
    with pytest.raises(ValueError):
        publication(data, page_size=5 if defect == "duplicate_cross_page" else 5000)


def test_physical_pagination_keeps_executor_and_stop_propagation():
    item, calls = publication(page_size=5)
    assert len(calls) == 8 and len(item.sessions) == 38
    assert [call["offset"] for call in calls] == list(range(0, 40, 5))

    class Stop(BaseException):
        pass

    class Client:
        def query(self, *args, **kwargs):
            pytest.fail("executor stop must precede request")

    def stop(*args):
        raise Stop()

    with pytest.raises(Stop):
        load_calendar(SYMBOL, str(START), str(END), provider=TushareProvider(Client()), request_executor=stop)


@pytest.mark.parametrize("symbol", [None, "600519.SS", "000001.SZ"])
def test_scope_does_not_generalize(settings, symbol):
    persist(settings)
    with MarketHistoryStore.open(settings[1]) as store:
        assert cache.read_prepared_sse_calendar(store, symbol, START, END) is None
    with MarketHistoryStore.open(replace(settings[1], mode=MarketHistoryMode.AUTHORITATIVE)) as store:
        assert cache.read_prepared_sse_calendar(store, SYMBOL, START, END) is None


@pytest.mark.parametrize("statement", [
    "UPDATE provider_datasets SET provider_name='baostock'",
    "UPDATE upstream_services SET account_scope='other-account'",
    "UPDATE market_session_calendars SET reference_market='SZSE'",
    "UPDATE ingestion_runs SET operation='seed'",
    "UPDATE market_sessions SET session_status='closed',opens_at=NULL,closes_at=NULL WHERE session_date='2026-09-01'",
    "DELETE FROM market_sessions WHERE session_date='2026-09-02'",
])
def test_persisted_source_or_membership_corruption_refused(settings, statement):
    persist(settings)
    with MarketHistoryStore.open(settings[1]) as store:
        store._connection.execute(statement)
        store._connection.commit()
    with MarketHistoryStore.open(settings[1]) as store, pytest.raises(ValueError):
        cache.read_prepared_sse_calendar(store, SYMBOL, START, END)


def test_new_partial_revision_does_not_union_or_revert(settings):
    persist(settings)
    partial, _ = publication(start=START + timedelta(days=1))
    persist(settings, replace(partial, observed_at=OBSERVED + timedelta(seconds=1)))
    with (MarketHistoryStore.open(settings[1]) as store,
          pytest.raises(ValueError, match="range incomplete")):
        cache.read_prepared_sse_calendar(store, SYMBOL, START, END)


@pytest.mark.usefixtures("validated_manifest")
def test_public_snapshot_and_second_cache_hit(settings, monkeypatch):
    # Prepared calendar is broader than this price/cache window.
    broader, _ = publication(start=START - timedelta(days=2), end=END + timedelta(days=2))
    persist(settings, broader)
    frame = pd.DataFrame({"Date": pd.bdate_range(START, END),
        "Open": 10.0, "High": 12.0, "Low": 9.0, "Close": 11.0, "Volume": 100.0})
    calls = []

    def load(symbol, start, end, *, physical_request=None, **kwargs):
        assert symbol == SYMBOL and (start, end) == (str(START), str(END))
        calls.append("price")
        return physical_request("synthetic-price", lambda: frame.copy())

    from tradingagents.market_history import calendar_discovery
    monkeypatch.setattr(calendar_discovery, "ensure_mainland_calendar_window",
        lambda *a, **kw: pytest.fail("Tushare preparation must avoid BaoStock acquisition"))
    monkeypatch.setitem(market_snapshot.SNAPSHOT_PROVIDERS, "akshare",
        replace(market_snapshot.SNAPSHOT_PROVIDERS["akshare"], load=load,
                uses_typed_physical_requests=True, history_load=None))
    live = market_snapshot.get_authoritative_market_snapshot(SYMBOL, str(START), str(END), minimum_history_rows=21)
    # Existing adjusted-frame behavior retains the initial miss diagnostic.
    assert live.history_store_status == "degraded"
    assert live.history_store_diagnostic.startswith("current_cache_miss:")
    assert "write_failed" not in live.history_store_diagnostic
    cached = market_snapshot.get_authoritative_market_snapshot(SYMBOL, str(START), str(END), minimum_history_rows=21)
    assert cached.history_store_status == "current_only_cached", cached.history_store_diagnostic
    assert calls == ["price"] and len(live.physical_attempt_events) == 1
    assert not cached.physical_attempt_events
    assert cached.snapshot_id == live.snapshot_id and cached.retrieved_at == live.retrieved_at
    issues, checked = [], []
    budget_readiness._inspect_market(settings[0], SYMBOL, START, END, issues, checked)
    assert not issues
    assert any(item.location == "market_cache.akshare"
               and item.status == "evidence_checked_runtime_revalidation_required"
               for item in checked)
    with MarketHistoryStore.open(settings[1]) as store:
        calendar = cache.read_prepared_sse_calendar(store, SYMBOL, START, END)
    with pytest.raises(ValueError, match="instrument scope"):
        cache.validate_coverage(frame, cache.calendar_evidence(calendar), str(START), str(END), symbol="000001.SZ")


def test_latest_revision_with_no_members_cannot_revert(settings):
    persist(settings)
    newer, _ = publication(start=START + timedelta(days=1))
    _, revision = persist(settings, replace(newer, observed_at=OBSERVED + timedelta(seconds=1)))
    with MarketHistoryStore.open(settings[1]) as store:
        store._connection.execute("DELETE FROM market_sessions WHERE calendar_revision_id=?", (revision.calendar_revision_id,))
        store._connection.commit()
    with (MarketHistoryStore.open(settings[1]) as store,
          pytest.raises(ValueError, match="membership invalid")):
        cache.read_prepared_sse_calendar(store, SYMBOL, START, END)


def persist_baostock(settings, *, start=START, end=END):
    """Real CAL-02 acquisition/publication with a wholly synthetic SDK transport."""
    from tests.test_baostock_session_calendar_acquisition import (
        _coordinator,
        _FakeBaoStockTransport,
        _request,
    )
    from tradingagents.dataflows.baostock_calendar import acquire_mainland_session_calendar
    from tradingagents.market_history.calendar import (
        publication_from_baostock_session_calendar_acquisition,
    )

    transport = _FakeBaoStockTransport(rows=tuple(
        (str(day.date()), str(int(day.weekday() < 5))) for day in pd.date_range(start, end)))
    with MarketHistoryStore.open_provider_request_authority(settings[1]) as authority:
        count = authority._connection.execute("SELECT COUNT(*) FROM provider_request_attempts").fetchone()[0]
        acquisition = acquire_mainland_session_calendar(
            _request(start_date=start, end_date=end), coordinator=_coordinator(authority),
            transport_factory=lambda: transport, owner_id=f"synthetic-coexist-{count}",
            now=lambda: OBSERVED + timedelta(minutes=count + 1), sleep=lambda _: None,
            lease_duration=timedelta(seconds=30))
    assert acquisition.physical_attempt_count == 3
    publication = publication_from_baostock_session_calendar_acquisition(acquisition)
    with MarketHistoryStore.open(settings[1]) as store:
        result = store.publish_session_calendar(publication)
        store.read_session_calendar_revision(result.calendar_revision_id)
    return result


@pytest.mark.parametrize("symbol", ["601328.SS", "600895.SS", "600519.SS", "000001.SZ"])
@pytest.mark.parametrize("tushare_first", [False, True])
@pytest.mark.usefixtures("validated_manifest")
def test_mixed_sources_preflight_actual_snapshot_and_cache(settings, monkeypatch, symbol, tushare_first):
    from tradingagents.market_history import calendar_discovery

    # Deliberately distinct synthetic membership proves the two sources are not blended.
    data = rows()
    for row in data:
        if row["cal_date"] == "20260908":
            row["is_open"] = "0"
    tushare = publication(data)[0]
    if tushare_first:
        _, sse = persist(settings, tushare)
    bao = persist_baostock(settings)
    if not tushare_first:
        _, sse = persist(settings, tushare)

    def no_calendar_acquisition(*args, **kwargs):
        pytest.fail("complete mixed store must not acquire a calendar")

    monkeypatch.setattr(calendar_discovery, "acquire_mainland_session_calendar", no_calendar_acquisition)
    with MarketHistoryStore.open(settings[1]) as store:
        old_view = calendar_discovery.read_mainland_calendar_window(store, START, END)
        assert old_view.source_calendar_revision_ids == (bao.calendar_revision_id,)
        assert sse.calendar_revision_id not in old_view.source_calendar_revision_ids
    selected = market_snapshot._ensure_analysis_calendar(str(START), str(END), symbol=symbol)
    if symbol == SYMBOL:
        assert selected.calendar_revision_id == sse.calendar_revision_id
    else:
        assert selected.source_calendar_revision_ids == (bao.calendar_revision_id,)
    issues, checked = [], []
    budget_readiness._inspect_market(settings[0], symbol, START, END, issues, checked)
    assert not issues
    assert any(item.location == "calendar" and item.requires_provider_request is False for item in checked)

    days = pd.bdate_range(START, END)
    if symbol == SYMBOL:
        days = days[days != pd.Timestamp("2026-09-08")]
    frame = pd.DataFrame({"Date": days, "Open": 10., "High": 12., "Low": 9., "Close": 11., "Volume": 100.})
    calls = []

    def price(actual_symbol, start, end, *, physical_request=None, **kwargs):
        assert actual_symbol == symbol and (start, end) == (str(START), str(END))
        calls.append(actual_symbol)
        return physical_request("synthetic-price", lambda: frame.copy())

    monkeypatch.setitem(market_snapshot.SNAPSHOT_PROVIDERS, "akshare",
        replace(market_snapshot.SNAPSHOT_PROVIDERS["akshare"], load=price,
                uses_typed_physical_requests=True, history_load=None))
    live = market_snapshot.get_authoritative_market_snapshot(symbol, str(START), str(END), minimum_history_rows=21)
    cached = market_snapshot.get_authoritative_market_snapshot(symbol, str(START), str(END), minimum_history_rows=21)
    assert len(live.frame) == len(frame)
    assert cached.history_store_status == "current_only_cached", cached.history_store_diagnostic
    assert calls == [symbol] and len(live.physical_attempt_events) == 1
    assert not cached.physical_attempt_events
    assert (cached.snapshot_id, cached.retrieved_at) == (live.snapshot_id, live.retrieved_at)
    issues, checked = [], []
    budget_readiness._inspect_market(settings[0], symbol, START, END, issues, checked)
    assert not issues
    assert any(item.location == "market_cache.akshare"
               and item.status == "evidence_checked_runtime_revalidation_required" for item in checked)


@pytest.mark.parametrize("defect", ["membership", "empty", "provider_name", "dataset_rebinding", "outside"])
def test_mixed_store_does_not_hide_damaged_baostock(settings, monkeypatch, defect):
    from tradingagents.market_history import calendar_discovery

    _, sse = persist(settings)
    persist_baostock(settings)  # Complete valid coverage must not mask the damaged revision below.
    damaged = persist_baostock(settings, start=START + timedelta(days=1), end=END - timedelta(days=1))
    with MarketHistoryStore.open(settings[1]) as store:
        revision = damaged.calendar_revision_id
        if defect == "membership":
            store._connection.execute("UPDATE market_sessions SET session_status='closed',opens_at=NULL,closes_at=NULL "
                "WHERE calendar_revision_id=? AND session_date='2026-09-02'", (revision,))
        elif defect == "empty":
            store._connection.execute("DELETE FROM market_sessions WHERE calendar_revision_id=?", (revision,))
        elif defect == "outside":
            store._connection.execute("UPDATE market_sessions SET session_date=replace(session_date,'2026','2025') "
                "WHERE calendar_revision_id=?", (revision,))
        elif defect == "provider_name":
            store._connection.execute("UPDATE provider_datasets SET provider_name='tushare' "
                "WHERE provider_dataset_id=(SELECT provider_dataset_id FROM market_session_calendars "
                "WHERE calendar_revision_id=?)", (revision,))
        else:
            store._connection.execute("UPDATE market_session_calendars SET provider_dataset_id="
                "(SELECT provider_dataset_id FROM market_session_calendars WHERE calendar_revision_id=?) "
                "WHERE calendar_revision_id=?", (sse.calendar_revision_id, revision))
        store._connection.commit()

    with MarketHistoryStore.open(settings[1]) as store, pytest.raises(MarketSessionCalendarIntegrityError):
        calendar_discovery.read_mainland_calendar_window(store, START, END)
    issues, checked = [], []
    budget_readiness._inspect_market(settings[0], "600895.SS", START, END, issues, checked)
    assert any(item.location == "calendar" and item.reason == "corrupt_or_unreadable" for item in issues)
    monkeypatch.setattr(calendar_discovery, "acquire_mainland_session_calendar",
        lambda *a, **kw: pytest.fail("corruption must not become permission to reacquire"))
    with pytest.raises(MarketSessionCalendarIntegrityError):
        market_snapshot._ensure_analysis_calendar(str(START), str(END), symbol="600895.SS")


def test_tushare_only_does_not_admit_calendar_to_other_symbols(settings):
    from tradingagents.market_history.calendar_discovery import read_mainland_calendar_window

    persist(settings)
    with MarketHistoryStore.open(settings[1]) as store:
        assert read_mainland_calendar_window(store, START, END) is None
    issues, checked = [], []
    budget_readiness._inspect_market(settings[0], "600895.SS", START, END, issues, checked)
    assert any(item.location == "calendar" and item.reason == "incomplete_natural_date_window" for item in issues)


@pytest.mark.parametrize("mode", [MarketHistoryMode.CURRENT, MarketHistoryMode.AUTHORITATIVE])
def test_legacy_baostock_calendar_coexists_without_readmission(settings, mode):
    from tradingagents.market_history.calendar import (
        MarketSession,
        MarketSessionCalendarPublication,
        MarketSessionStatus,
    )
    from tradingagents.market_history.calendar_discovery import read_mainland_calendar_window
    from tradingagents.market_history.current import baostock_history_binding
    from tradingagents.market_history.models import ProvenanceClass

    persist(settings)
    provider, _ = baostock_history_binding("600895.SS")
    legacy = MarketSessionCalendarPublication(
        provider=replace(provider, provider_dataset_id="synthetic-legacy-baostock-calendar",
                         dataset_name="synthetic-legacy-baostock-calendar"),
        reference_market="mainland-cn", timezone_name="Asia/Shanghai", observed_at=OBSERVED,
        provenance_class=ProvenanceClass.RETROSPECTIVE_BACKFILL,
        raw_payload=b'{"source":"synthetic legacy BaoStock transport"}',
        sessions=tuple(MarketSession(day.date(), MarketSessionStatus.OPEN if day.weekday() < 5
                                    else MarketSessionStatus.CLOSED) for day in pd.date_range(START, END)))
    with MarketHistoryStore.open(settings[1]) as store:
        revision = store.publish_session_calendar(legacy)
    settings[0]["market_history_mode"] = mode.value
    with MarketHistoryStore.open(replace(settings[1], mode=mode)) as store:
        view = read_mainland_calendar_window(store, START, END)
        assert view.source_calendar_revision_ids == (revision.calendar_revision_id,)
    selected = market_snapshot._ensure_analysis_calendar(str(START), str(END), symbol="600895.SS")
    assert selected.source_calendar_revision_ids == (revision.calendar_revision_id,)
    if mode is MarketHistoryMode.CURRENT:
        issues, checked = [], []
        budget_readiness._inspect_market(settings[0], "600895.SS", START, END, issues, checked)
        assert not issues

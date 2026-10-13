"""Synthetic CURRENT raw-history tests; no clients, credentials or subprocesses."""
from dataclasses import replace
from datetime import date, datetime, timedelta, timezone

import pandas as pd
import pytest

from tradingagents.dataflows import config as configuration, tushare_market_history as adapter
from tradingagents.market_history import current
from tradingagents.market_history.config import MarketHistoryConfig
from tradingagents.market_history.current_incremental import load_current_history
from tradingagents.market_history.models import (
    SnapshotPurpose,
    StrictReplayUnavailable,
)
from tradingagents.market_history.store import MarketHistoryStore

SYMBOL = "600519.SS"
NOW = datetime(2026, 10, 9, 10, tzinfo=timezone.utc)
DAYS = tuple(value.date() for value in pd.bdate_range(end="2026-10-08", periods=1100))


@pytest.fixture
def rig(tmp_path, monkeypatch):
    runtime = configuration.get_config()
    runtime.update(market_history_mode="current", data_usage_mode="personal_research",
        market_history_database_path=str(tmp_path / "history.sqlite3"),
        market_history_payload_root=str(tmp_path / "payloads"),
        market_history_backup_root=str(tmp_path / "backups"))
    monkeypatch.setattr(configuration, "_config", runtime)
    clock = [NOW]
    class FrozenDatetime(datetime):
        @classmethod
        def now(cls, tz=None):
            return clock[0].astimezone(tz) if tz else clock[0].replace(tzinfo=None)
    monkeypatch.setattr(adapter, "datetime", FrozenDatetime)
    monkeypatch.setattr(current, "_now", lambda: clock[0])
    provider, instrument = adapter.history_binding(SYMBOL)
    calls = []
    changed = set()
    omissions = set()
    factor_change = [None]
    failure = [False]
    suspensions = set()

    def calendar(first, last):
        return {value.date(): "1" if value.date() in DAYS else "0"
                for value in pd.date_range(first, last)}

    calendar_calls = []
    class CalendarSource:
        def fetch_range(self, endpoint, **kwargs):
            calendar_calls.append((kwargs["start_date"], kwargs["end_date"]))
            return pd.DataFrame([{"exchange": "SSE", "cal_date": day.strftime("%Y%m%d"), "is_open": flag}
                for day, flag in calendar(kwargs["start_date"], kwargs["end_date"]).items()])
    def acquire_calendar(first, last):
        return adapter.load_calendar(SYMBOL, first, last, provider=CalendarSource())

    def acquire(first, last, anchor):
        calls.append((first, last, anchor))
        if failure[0]:
            raise ValueError("synthetic incomplete page")
        raw = pd.DataFrame([{"ts_code": "600519.SH", "trade_date": day.strftime("%Y%m%d"),
            "open": "10", "high": "14", "low": "9", "close": "12" if day in changed else "11", "vol": "1"}
            for day in DAYS if first <= day.isoformat() <= last and day not in omissions])
        factors = pd.DataFrame([{"ts_code": "600519.SH", "trade_date": day.strftime("%Y%m%d"),
            "adj_factor": "2" if factor_change[0] is not None and day >= factor_change[0] else "1"}
            for day in DAYS if day.isoformat() <= anchor])
        cal = pd.DataFrame([{"exchange": "SSE", "cal_date": day.strftime("%Y%m%d"), "is_open": flag}
                            for day, flag in calendar(first, last).items()])
        suspension_rows = pd.DataFrame([{"ts_code": "600519.SH", "trade_date": day.strftime("%Y%m%d"),
            "suspend_type": "S", "suspend_timing": None} for day in suspensions if first <= day.isoformat() <= last])
        return adapter._candidate(raw, factors, suspension_rows, cal, binding=provider,
            instrument=instrument, ts_code="600519.SH", exchange="SSE",
            start=date.fromisoformat(first), end=date.fromisoformat(last), factor_end=date.fromisoformat(anchor))

    config = MarketHistoryConfig.from_mapping(runtime)
    def load(first, last, **kwargs):
        with MarketHistoryStore.open(config) as store:
            return load_current_history(store, SYMBOL, first.isoformat(), last.isoformat(),
                acquire=acquire, acquire_calendar=acquire_calendar, now=kwargs.pop("now", clock[0]), **kwargs)
    load.calendar_calls = calendar_calls
    load.suspensions = suspensions
    load.clock = clock
    return config, load, calls, changed, omissions, factor_change, failure


def test_reopen_260_rows_then_shift_shrink_and_retain_1000(rig):
    config, load, calls, *_ = rig
    first = load(DAYS[0], DAYS[259])
    again = load(DAYS[0], DAYS[259])
    assert len(first.frame) == len(again.frame) == 260
    assert len(calls) == 1 and again.history_requested_ranges == ()
    assert again.history_bundle.observed_at == first.history_bundle.observed_at
    extended = load(DAYS[0], DAYS[999])
    assert len(extended.frame) == 1000
    count = len(calls)
    shrunk = load(DAYS[400], DAYS[799])
    assert len(shrunk.frame) == 400 and len(calls) == count
    with MarketHistoryStore.open(config) as store:
        retained = store.load_current_history(*adapter.history_binding(SYMBOL))
        assert len(retained.raw) == 1000
        assert store.read_payload(retained.source_digests[0])


def test_new_session_refreshes_exactly_latest_21_and_returns_full_window(rig):
    _, load, calls, changed, *_ = rig
    load(DAYS[0], DAYS[259])
    changed.add(DAYS[250])
    updated = load(DAYS[0], DAYS[260])
    assert calls[-1][:2] == (DAYS[240].isoformat(), DAYS[260].isoformat())
    assert len(updated.frame) == 261
    assert updated.frame.loc[updated.frame.Date == pd.Timestamp(DAYS[250]), "Close"].item() == 12
    count = len(calls)
    load(DAYS[0], DAYS[260])
    assert len(calls) == count


def test_disjoint_holes_do_not_refetch_retained_middle(rig):
    _, load, calls, *_ = rig
    load(DAYS[0], DAYS[99])
    load(DAYS[101], DAYS[199])
    load(DAYS[201], DAYS[259])
    calls.clear()
    result = load(DAYS[0], DAYS[259])
    assert [item[:2] for item in calls] == [(DAYS[100].isoformat(), DAYS[100].isoformat()),
                                           (DAYS[200].isoformat(), DAYS[200].isoformat())]
    assert len(result.frame) == 260


def test_factor_revision_rebuilds_old_raw_and_preserves_old_payload(rig):
    config, load, calls, _, _, factor, _ = rig
    old = load(DAYS[0], DAYS[259])
    with MarketHistoryStore.open(config) as store:
        before = store.load_current_history(*adapter.history_binding(SYMBOL))
        evidence = {digest: store.read_payload(digest) for digest in before.source_digests}
    factor[0] = DAYS[250]
    new = load(DAYS[0], DAYS[259], force_refresh=True)
    assert calls[-1][:2] == (DAYS[239].isoformat(), DAYS[259].isoformat())
    assert old.frame.iloc[0].Close == 11 and new.frame.iloc[0].Close == 5.5
    assert new.frame.iloc[-1].Close == 11
    with MarketHistoryStore.open(config) as store:
        assert all(store.read_payload(digest) == raw for digest, raw in evidence.items())
        assert store._connection.execute("SELECT COUNT(*) FROM history_bundle_revisions").fetchone()[0] == 2


def test_explicit_repair_only_refreshes_requested_old_session(rig):
    _, load, calls, changed, *_ = rig
    load(DAYS[0], DAYS[259])
    changed.add(DAYS[10])
    result = load(DAYS[0], DAYS[259], repair_ranges=[(DAYS[10].isoformat(), DAYS[10].isoformat())])
    assert calls[-1][:2] == (DAYS[10].isoformat(), DAYS[10].isoformat())
    assert result.frame.iloc[10].Close == 12


@pytest.mark.parametrize("failure_kind", ["page", "gap", "publish"])
def test_failed_refresh_does_not_advance_committed_coverage(rig, monkeypatch, failure_kind):
    config, load, _, _, omissions, _, failure = rig
    old = load(DAYS[0], DAYS[259])
    if failure_kind == "page":
        failure[0] = True
    elif failure_kind == "gap":
        omissions.add(DAYS[260])
    else:
        def reject(*args, **kwargs):
            raise ValueError("synthetic publication failure")
        monkeypatch.setattr(MarketHistoryStore, "publish_current_history", reject)
    with pytest.raises(ValueError):
        load(DAYS[0], DAYS[260])
    with MarketHistoryStore.open(config) as store:
        retained = store.load_current_history(*adapter.history_binding(SYMBOL))
        assert retained.bundle_revision_id == old.bundle_revision_id
        assert len(retained.raw) == 260 and DAYS[260] not in retained.raw


def test_current_cannot_use_strict_publication_or_reconstruction(rig):
    config, load, *_ = rig
    candidate = load(DAYS[0], DAYS[259])
    with MarketHistoryStore.open(config) as store:
        with pytest.raises(ValueError, match="not qualified"):
            store.publish_history_bundle(candidate.history_bundle)
        for purpose in (SnapshotPurpose.CURRENT_ANALYSIS, SnapshotPurpose.STRICT_REPLAY):
            with pytest.raises(StrictReplayUnavailable, match="CURRENT-only"):
                store.reconstruct_snapshot(candidate.bundle_revision_id, requested_date=DAYS[259],
                    purpose=purpose, replay_as_of=NOW+timedelta(days=1))
        rows = store._connection.execute("SELECT DISTINCT provenance_class FROM raw_market_observation_revisions").fetchall()
        assert rows == [("retrospective_backfill",)]


def test_binding_change_and_stale_writer_fail_closed(rig):
    config, load, *_ = rig
    old = load(DAYS[0], DAYS[259])
    load(DAYS[0], DAYS[260])
    with MarketHistoryStore.open(config) as store:
        binding, instrument = adapter.history_binding(SYMBOL)
        assert store.load_current_history(replace(binding, provider_dataset_id="other-version"), instrument) is None
        with pytest.raises(ValueError, match="changed during acquisition"):
            store.publish_current_history(old.history_bundle, prior_bundle_revision_id=old.bundle_revision_id)


def test_expired_current_refreshes_21_sessions_without_dropping_old_history(rig):
    config, load, calls, *_ = rig
    load(DAYS[0], DAYS[259])
    with MarketHistoryStore.open(config) as store:
        assert len(store.load_current_history(*adapter.history_binding(SYMBOL)).raw) == 260
    result = load(DAYS[0], DAYS[259], now=NOW+timedelta(days=2))
    assert calls[-1][:2] == (DAYS[239].isoformat(), DAYS[259].isoformat())
    assert len(result.frame) == 260


def test_closed_calendar_extension_persists_without_new_raw_or_repeated_requests(rig):
    config, load, calls, *_ = rig
    old = load(DAYS[-260], DAYS[-1])
    extended_end = DAYS[-1]+timedelta(days=2)
    expanded = load(DAYS[-260], extended_end)
    assert expanded.bundle_revision_id == old.bundle_revision_id
    assert len(calls) == 1 and len(load.calendar_calls) == 1
    load(DAYS[-260], extended_end)
    assert len(calls) == 1 and len(load.calendar_calls) == 1
    with MarketHistoryStore.open(config) as store:
        retained = store.load_current_history(*adapter.history_binding(SYMBOL))
        assert retained.calendar[extended_end] == "0" and len(retained.calendar_revision_ids) == 1
        assert retained.observed_at == old.history_bundle.observed_at


def test_current_view_proof_is_explicit_and_immutable_after_new_bundle(rig):
    import json

    from tradingagents.market_history.current_cache import (
        calendar_evidence,
        validate_calendar_evidence,
    )
    _, load, *_ = rig
    old = load(DAYS[0], DAYS[259])
    evidence = calendar_evidence(old.calendar)
    assert evidence["proof"]["kind"] == "tushare-current-view-v1"
    manifest = json.loads(evidence["proof"]["raw_payload"])
    assert manifest["bundle_revision_id"] == old.bundle_revision_id and manifest["source_digests"]
    load(DAYS[0], DAYS[260])
    validate_calendar_evidence(evidence)


def test_suspension_without_official_carried_close_cannot_become_coverage(rig):
    config, load, _, _, omissions, *_ = rig
    old = load(DAYS[0], DAYS[259])
    omissions.add(DAYS[260])
    load.suspensions.add(DAYS[260])
    with pytest.raises(ValueError, match="no official suspension carried close"):
        load(DAYS[0], DAYS[260])
    with MarketHistoryStore.open(config) as store:
        assert store.load_current_history(*adapter.history_binding(SYMBOL)).bundle_revision_id == old.bundle_revision_id


def test_old_repairs_do_not_renew_recent_raw_freshness(rig):
    _, load, calls, *_ = rig
    load(DAYS[0], DAYS[259])
    load.clock[0] = NOW + timedelta(hours=23)
    load(DAYS[0], DAYS[259], repair_ranges=[(DAYS[10].isoformat(), DAYS[10].isoformat())])
    assert calls[-1][:2] == (DAYS[10].isoformat(), DAYS[10].isoformat())
    load.clock[0] = NOW + timedelta(hours=25)
    result = load(DAYS[0], DAYS[259])
    assert calls[-1][:2] == (DAYS[239].isoformat(), DAYS[259].isoformat())
    assert len(result.frame) == 260


def test_before_close_fixed_proof_survives_close_but_new_request_acquires_session(rig, monkeypatch):
    from tradingagents.market_history import current_cache

    _, load, calls, _, omissions, *_ = rig
    before_close = datetime(2026, 10, 8, 5, tzinfo=timezone.utc)
    load.clock[0] = before_close
    monkeypatch.setattr(current_cache, "_now", lambda: load.clock[0])
    omissions.add(DAYS[-1])
    old = load(DAYS[-260], DAYS[-1])
    assert len(old.frame) == 259
    proof = current_cache.calendar_evidence(old.calendar)
    current_cache.validate_calendar_evidence(proof)

    load.clock[0] = before_close + timedelta(hours=4)
    current_cache.validate_calendar_evidence(proof)
    omissions.remove(DAYS[-1])
    updated = load(DAYS[-260], DAYS[-1])
    assert calls[-1][:2] == (DAYS[-21].isoformat(), DAYS[-1].isoformat())
    assert len(updated.frame) == 260 and updated.bundle_revision_id != old.bundle_revision_id
    current_cache.validate_calendar_evidence(proof)


@pytest.fixture
def ordinary_rig(tmp_path, monkeypatch):
    """Real normal entry/store/coordinator/pagination; only the client is fake."""
    from threading import Event, RLock
    from types import SimpleNamespace

    from tradingagents.dataflows import acquisition, market_snapshot
    from tradingagents.market_history import coordinator, current_cache, current_incremental
    from tradingagents.picker.tushare_provider import TushareProvider

    clock = [NOW]
    lock = RLock()
    calls = []
    blocked = [False]
    entered, release, follower = Event(), Event(), Event()
    runtime = configuration.get_config()
    runtime.update(market_history_mode="current", data_usage_mode="personal_research",
        mainland_capability_routing_mode="legacy",
        market_history_database_path=str(tmp_path / "history.sqlite3"),
        market_history_payload_root=str(tmp_path / "payloads"),
        market_history_backup_root=str(tmp_path / "backups"),
        market_data_vendors={"cn_a": {"core_stock_apis": "tushare"}})
    monkeypatch.setattr(configuration, "_config", runtime)
    class FrozenDatetime(datetime):
        @classmethod
        def now(cls, tz=None):
            return clock[0].astimezone(tz) if tz else clock[0].replace(tzinfo=None)
    for module in (adapter, market_snapshot, acquisition, current_incremental):
        monkeypatch.setattr(module, "datetime", FrozenDatetime)
    monkeypatch.setattr(current, "_now", lambda: clock[0])
    monkeypatch.setattr(current_cache, "_now", lambda: clock[0])
    monkeypatch.setattr(market_snapshot, "_coordinator_now", lambda: clock[0])
    def sleep(seconds):
        # A persisted-flight waiter yields to its peer without expiring its
        # lease; pacing advances the isolated synthetic clock without sleeping.
        if seconds <= 0.05:
            Event().wait(0.001)
        else:
            with lock:
                clock[0] += timedelta(seconds=seconds)
    monkeypatch.setattr(market_snapshot, "_coordinator_sleep", sleep)
    monkeypatch.setattr(current_cache, "compatibility_diagnostic", lambda: None)
    monkeypatch.setattr(adapter.PITConfig, "from_env", lambda: None)
    factor_days = tuple(value.date() for value in pd.bdate_range(end="2026-10-08", periods=6000))
    class Client:
        def query(self, endpoint, **kwargs):
            with lock:
                calls.append((endpoint, dict(kwargs)))
                pause = blocked[0] and endpoint == "daily" and not entered.is_set()
                if pause:
                    entered.set()
            if pause and not release.wait(10):
                raise AssertionError("synthetic concurrency release timed out")
            code = kwargs.get("ts_code")
            lower = date.fromisoformat("1990-01-01") if "start_date" not in kwargs else datetime.strptime(kwargs["start_date"], "%Y%m%d").date()
            upper = datetime.strptime(kwargs["end_date"], "%Y%m%d").date()
            days = [day for day in factor_days if lower <= day <= upper]
            if endpoint == "daily":
                rows = [{"ts_code": code, "trade_date": day.strftime("%Y%m%d"),
                         "open": "10", "high": "12", "low": "9", "close": "11", "vol": "1"} for day in days]
            elif endpoint == "adj_factor":
                rows = [{"ts_code": code, "trade_date": day.strftime("%Y%m%d"), "adj_factor": "1"} for day in days]
            elif endpoint == "suspend_d":
                rows = []
            elif endpoint == "trade_cal":
                rows = [{"exchange": kwargs["exchange"], "cal_date": day.strftime("%Y%m%d"),
                         "is_open": int(day.weekday() < 5)} for day in pd.date_range(lower, upper)]
            else:
                raise AssertionError("unexpected synthetic endpoint")
            assert kwargs["limit"] == 5000
            return pd.DataFrame(rows[kwargs["offset"]:kwargs["offset"]+kwargs["limit"]])
    synthetic_client = Client()
    monkeypatch.setattr(TushareProvider, "create", lambda *args, **kwargs: TushareProvider(synthetic_client, page_size=5000))
    # Separate flight maps exercise persisted SQLite lease/result handoff as if
    # different processes, while the test launches no child process.
    original_coordinator = coordinator.ProviderRequestCoordinator
    class SeparateCoordinator(original_coordinator):
        def __init__(self, *args, **kwargs):
            super().__init__(*args, **kwargs)
            self._single_flights = {}
            self._single_flight_lock = RLock()
        def _wait_for_persisted_sequence(self, **kwargs):
            follower.set()
            return super()._wait_for_persisted_sequence(**kwargs)
    monkeypatch.setattr(coordinator, "ProviderRequestCoordinator", SeparateCoordinator)
    config = MarketHistoryConfig.from_mapping(runtime)
    with MarketHistoryStore.open(config), MarketHistoryStore.open_provider_request_authority(config):
        pass
    def get(symbol="600895.SH", first="2021-10-08", last="2026-10-08"):
        with market_snapshot.authoritative_snapshot_run():
            return market_snapshot.get_authoritative_market_snapshot(symbol, first, last, minimum_history_rows=200)
    def events():
        with MarketHistoryStore.open_provider_request_authority(config) as store:
            return store._connection.execute(
                "SELECT attempt_event_id,operation,outcome FROM provider_request_attempts ORDER BY rowid").fetchall()
    return SimpleNamespace(get=get, calls=calls, events=events, config=config, runtime=runtime,
        blocked=blocked, entered=entered, release=release, follower=follower, clock=clock,
        synthetic_client=synthetic_client)


@pytest.mark.parametrize("symbol", ["600895.SH", "000001.SZ"])
def test_legacy_shadow_identity_preserved_by_current_publication_and_reopen(ordinary_rig, symbol):
    from hashlib import sha256

    from tradingagents.market_history.shadow import persist_mainland_provider_frame

    rig = ordinary_rig
    text = "Date,Open,High,Low,Close,Volume\n2026-10-08,10,12,9,11,100\n"
    legacy = persist_mainland_provider_frame(symbol=symbol, provider="akshare",
        adjustment_basis="qfq", requested_start="2026-10-08", requested_end="2026-10-08",
        effective_trading_date="2026-10-08", retrieved_at=NOW.isoformat(),
        snapshot_id="synthetic-legacy:" + symbol, frame_digest=sha256(text.encode()).hexdigest(),
        history_rows=1, normalized_frame_text=text)
    assert legacy.persisted
    with MarketHistoryStore.open(rig.config) as store:
        old = store.load_provider_frame_revision(legacy.frame_revision_id)
        old_payload = store.read_payload(old.stored.payload_digest)
    first = rig.get(symbol)
    assert first.history_store_status == "current_only_refreshed", first.history_store_diagnostic
    before = rig.events()
    restored = rig.get(symbol)
    assert restored.history_store_status == "current_only_cached", restored.history_store_diagnostic
    assert (restored.snapshot_id, restored.frame_sha256, restored.retrieved_at,
            restored.current_history_lineage) == (first.snapshot_id, first.frame_sha256,
            first.retrieved_at, first.current_history_lineage)
    assert len(rig.calls) == 5 and rig.events() == before
    with MarketHistoryStore.open(rig.config) as store:
        retained_legacy = store.load_provider_frame_revision(legacy.frame_revision_id)
        assert retained_legacy.stored == old.stored and retained_legacy.metadata == old.metadata
        pd.testing.assert_frame_equal(retained_legacy.frame, old.frame)
        assert store.read_payload(old.stored.payload_digest) == old_payload
        rows = store._connection.execute(
            "SELECT f.instrument_id FROM provider_frame_revisions f JOIN provider_datasets d "
            "USING(provider_dataset_id) WHERE d.provider_name='tushare'").fetchall()
        assert rows and {row[0] for row in rows} == {old.stored.instrument_id}
        assert store._connection.execute("PRAGMA foreign_key_check").fetchall() == []


def test_normal_five_year_two_stock_5000_page_requirement_is_ten_and_reopen_zero(ordinary_rig):
    rig = ordinary_rig
    first = [rig.get(symbol) for symbol in ("600895.SH", "000001.SZ")]
    expected_rows = len(pd.bdate_range("2021-10-08", "2026-10-08"))
    assert all(len(value.frame) == expected_rows > 200 for value in first)
    assert len(rig.calls) == len(rig.events()) == 10 <= 24
    assert [endpoint for endpoint, _ in rig.calls] == ["daily", "adj_factor", "adj_factor", "suspend_d", "trade_cal"] * 2
    assert [args["offset"] for endpoint, args in rig.calls if endpoint == "adj_factor"] == [0, 5000, 0, 5000]
    assert all("start_date" not in args for endpoint, args in rig.calls if endpoint == "adj_factor")
    before = rig.events()
    for symbol, original in zip(("600895.SH", "000001.SZ"), first, strict=True):
        restored = rig.get(symbol)
        assert (restored.snapshot_id, restored.frame_sha256, restored.retrieved_at, restored.current_history_lineage) == (
            original.snapshot_id, original.frame_sha256, original.retrieved_at, original.current_history_lineage)
        assert restored.history_store_status == "current_only_cached"
        assert restored.physical_attempt_events == ()
    assert rig.events() == before and len(rig.calls) == 10


@pytest.mark.parametrize("change", ["unknown_id", "currency", "identity_revision", "other_venue_id"])
def test_current_identity_conflicts_rejected_without_mutation(ordinary_rig, change):
    from tradingagents.dataflows import market_snapshot
    from tradingagents.market_history import current_cache
    from tradingagents.market_history.shadow import _stable_id

    rig = ordinary_rig
    descriptor = market_snapshot.SNAPSHOT_PROVIDERS["tushare"]
    _, instrument = current_cache.binding("600895.SH", "tushare", descriptor)
    stored_id = _stable_id("instrument", "600895.SS", "shanghai", "equity", "CNY")
    if change == "unknown_id":
        stored_id = "unrecognized-instrument"
    elif change == "other_venue_id":
        stored_id = _stable_id("instrument", "600895.SS", "shenzhen", "equity", "CNY")
    with MarketHistoryStore.open(rig.config) as store:
        values = (stored_id, instrument.canonical_symbol, instrument.reference_market,
            instrument.instrument_kind, "USD" if change == "currency" else "CNY",
            "unknown-v2" if change == "identity_revision" else instrument.identity_revision, NOW.isoformat())
        store._connection.execute("INSERT INTO instruments "
            "(instrument_id,canonical_symbol,reference_market,instrument_kind,currency,identity_revision,created_at) "
            "VALUES (?,?,?,?,?,?,?)", values)
        before = store._connection.total_changes
        with pytest.raises(ValueError, match="current instrument identity"):
            current_cache.binding("600895.SH", "tushare", descriptor, store=store)
        assert store._connection.total_changes == before
        assert store._connection.execute("SELECT COUNT(*) FROM provider_frame_revisions").fetchone() == (0,)
    assert rig.calls == []


@pytest.mark.parametrize("explicit_end,duplicate", [(None, False), (date(2026, 10, 8), False), (None, True)])
def test_acceptance_discovery_real_chain_then_reopen_or_reject_duplicate(ordinary_rig, monkeypatch, explicit_end, duplicate):
    import importlib.util
    import json
    from pathlib import Path
    from types import SimpleNamespace

    import tradingagents.tushare_acceptance as support
    from tradingagents import tushare_https as tls
    from tradingagents.dataflows import market_snapshot
    from tradingagents.market_history import coordinator
    from tradingagents.picker.tushare_provider import TushareProvider
    from tradingagents.tushare_acceptance import BASE, RequestGuard, run_data_phases

    rig = ordinary_rig
    records = []

    def sleep(seconds):
        rig.clock[0] += timedelta(seconds=seconds)

    discovery = support.CalendarDiscoveryWindow.capture(explicit_end, started_at=rig.clock[0])
    guard = RequestGuard(discovery.start.isoformat(), discovery.end.isoformat(), records.append,
        now=lambda: (rig.clock[0] - NOW).total_seconds(), sleep=sleep, discovery=discovery)

    class SyntheticTransport:
        def post(self, url, *, json, timeout):
            frame = rig.synthetic_client.query(json["api_name"], **json["params"])
            if duplicate and guard.discovering:
                frame = pd.concat([frame, frame.iloc[:1]], ignore_index=True)
            return SimpleNamespace(text=__import__("json").dumps({"code": 0, "data": {
                "fields": list(frame.columns), "items": frame.values.tolist()}}))

    monkeypatch.setattr(tls, "HTTPSPost", SyntheticTransport)
    # The fixture's coordinator subclass is discarded at teardown; explicitly
    # register its method bindings too so guard.bind never leaks across tests.
    cls = coordinator.ProviderRequestCoordinator
    monkeypatch.setattr(cls, "execute_direct_physical_request", cls.execute_direct_physical_request)
    monkeypatch.setattr(cls, "execute_physical_workflow", cls.execute_physical_workflow)
    guard.bind(tls, coordinator)

    def sdk_query(endpoint, fields="", **params):
        response = tls.HTTPSPost().post(BASE + "/" + endpoint,
            json={"api_name": endpoint, "token": "SYNTHETIC_NOT_A_CREDENTIAL",
                  "fields": fields, "params": {**params, "ts_type_name": BASE}})
        data = json.loads(response.text)["data"]
        return pd.DataFrame(data["items"], columns=data["fields"])

    monkeypatch.setattr(TushareProvider, "create",
        lambda *args, **kwargs: TushareProvider(tls._Client(sdk_query), page_size=5000))
    path = Path(__file__).resolve().parents[1] / "scripts/accept_tushare_current.py"
    spec = importlib.util.spec_from_file_location("synthetic_calendar_acceptance_launcher", path)
    launcher = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(launcher)
    if duplicate:
        with pytest.raises(support.AcceptanceStop, match="calendar_response_invalid"):
            launcher.discover_end(support, guard, rig.runtime)
        assert guard.attempts == len(rig.calls) == len(rig.events()) == 1
        assert guard.discovering and all(item["kind"] == "physical_attempt" for item in records)
        return
    selected = launcher.discover_end(support, guard, rig.runtime)
    assert selected == "2026-10-08" and guard.attempts == 2
    assert [(endpoint, args["exchange"]) for endpoint, args in rig.calls] == [("trade_cal", "SSE"), ("trade_cal", "SZSE")]
    run_data_phases(guard, market_snapshot, records.append, end=selected)
    assert guard.attempts == len(rig.calls) == len(rig.events()) == 12
    assert records[-1] == {"kind": "completed", "physical_attempts": 12,
                           "reuse_attempts": 0, "model_calls": 0}
    initial = [item for item in records if item["kind"] == "initial"]
    assert len(initial) == 2
    assert all(item["rows"] == len(pd.bdate_range("2021-10-08", "2026-10-08")) for item in initial)


def test_normal_current_simultaneous_requests_reuse_persisted_workflow(ordinary_rig):
    from concurrent.futures import ThreadPoolExecutor
    rig = ordinary_rig
    rig.blocked[0] = True
    with ThreadPoolExecutor(max_workers=2) as pool:
        leader = pool.submit(rig.get)
        try:
            assert rig.entered.wait(10)
            follower = pool.submit(rig.get)
            assert rig.follower.wait(10)
        finally:
            rig.release.set()
        left, right = leader.result(timeout=20), follower.result(timeout=20)
    assert (left.snapshot_id, left.frame_sha256, left.current_history_lineage) == (
        right.snapshot_id, right.frame_sha256, right.current_history_lineage)
    assert len(rig.calls) == len(rig.events()) == 5
    assert all(row[2] == "available" for row in rig.events())
    before = rig.events()
    assert rig.get().snapshot_id == left.snapshot_id
    assert rig.events() == before and len(rig.calls) == 5


def test_repair_force_and_normal_requests_have_distinct_workflow_identity(ordinary_rig):
    rig = ordinary_rig
    rig.get()
    rig.runtime["market_history_current_repair_ranges"] = [["2021-10-08", "2021-10-08"]]
    rig.get()
    rig.runtime["market_history_current_repair_ranges"] = []
    rig.runtime["market_history_current_force_refresh"] = True
    rig.get()
    with MarketHistoryStore.open_provider_request_authority(rig.config) as store:
        keys = store._connection.execute("SELECT DISTINCT request_key FROM provider_request_sequences").fetchall()
    assert len(keys) == 3
    daily = [args for endpoint, args in rig.calls if endpoint == "daily"]
    assert daily[1]["start_date"] == daily[1]["end_date"] == "20211008"
    assert daily[2]["start_date"] == pd.bdate_range(end="2026-10-08", periods=21)[0].strftime("%Y%m%d")


def test_denied_compatibility_gate_never_reads_retained_raw(ordinary_rig, monkeypatch):
    from tradingagents.market_history import current_cache
    rig = ordinary_rig
    rig.get()
    monkeypatch.setattr(current_cache, "compatibility_diagnostic", lambda: "synthetic_gate_denied")
    def reject(*args, **kwargs):
        raise AssertionError("gate-denied path read retained raw")
    monkeypatch.setattr(MarketHistoryStore, "load_current_history", reject)
    independent = rig.get()
    assert independent.current_history_lineage is None
    assert len(rig.calls) == len(rig.events()) == 10
    daily = [args for endpoint, args in rig.calls if endpoint == "daily"]
    assert len(daily) == 2 and daily[0] == daily[1]


def test_workflow_24_operation_limit_preserves_prior_complete_bundle(ordinary_rig):
    from tradingagents.dataflows.errors import NoMarketDataError
    rig = ordinary_rig
    original = rig.get()
    days = pd.bdate_range("2021-10-08", periods=100)
    rig.runtime["market_history_current_repair_ranges"] = [
        [days[index].strftime("%Y-%m-%d")] * 2 for index in (0, 10, 20, 30, 40, 50, 60)]
    with pytest.raises(NoMarketDataError):
        rig.get()
    assert len(rig.calls) == len(rig.events()) == 5 + 24
    rig.runtime["market_history_current_repair_ranges"] = []
    restored = rig.get()
    assert restored.snapshot_id == original.snapshot_id
    assert len(rig.calls) == len(rig.events()) == 29


@pytest.mark.parametrize("stop_kind", ["interrupt", "cooldown"])
def test_workflow_initial_pacing_opt_in_respects_stop_and_cooldown(ordinary_rig, stop_kind):
    from tradingagents.market_history import coordinator
    rig = ordinary_rig
    rig.get()
    calls_before = len(rig.calls)
    service, _ = coordinator.upstream_service_identity_for_provider("tushare")
    waits = []
    with MarketHistoryStore.open_provider_request_authority(rig.config) as store:
        owner = coordinator.ProviderRequestCoordinator(store)
        if stop_kind == "cooldown":
            owner.record_rate_limit(upstream_service_id=service, cooldown_scope="market-snapshot",
                observed_at=rig.clock[0], retry_after=timedelta(seconds=300), provider_code="synthetic_rate_limit")
        def wait(seconds):
            waits.append(seconds)
            owner.interrupt()
        with pytest.raises(coordinator.PhysicalAttemptBudgetExhausted) as captured:
            owner.execute_physical_workflow(request_key="synthetic-stop-workflow",
                upstream_service_id=service, owner_id="synthetic-stop-owner",
                priority=coordinator.RequestPriority.INTERACTIVE_MAINLAND,
                now=lambda: rig.clock[0], sleep=wait, lease_duration=timedelta(minutes=2),
                max_physical_operations=24, wait_for_initial_pacing=True,
                cooldown_scope="market-snapshot",
                workflow=lambda execute: pytest.fail("stopped workflow must not acquire data"))
        expected = coordinator.PhysicalAttemptOutcome.ABANDONED if stop_kind == "interrupt" else coordinator.PhysicalAttemptOutcome.RATE_LIMITED
        assert captured.value.failure.outcome is expected
        assert waits == ([1.5] if stop_kind == "interrupt" else [])
        assert store._connection.execute("SELECT COUNT(*) FROM request_queue WHERE request_key='synthetic-stop-workflow'").fetchone()[0] == 0
    assert len(rig.calls) == calls_before and len(rig.events()) == calls_before

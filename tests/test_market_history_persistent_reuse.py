"""Synthetic-only acceptance; standalone runner: scripts/run_current_cache_compatibility.py."""

import json
from dataclasses import dataclass, replace
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal

import pandas as pd
import pytest

from tradingagents.dataflows import config as config_module, market_snapshot as snapshot
from tradingagents.dataflows.errors import NoMarketDataError, VendorRateLimitError
from tradingagents.market_history import current, shadow
from tradingagents.market_history.calendar import (
    MarketSession,
    MarketSessionCalendarPublication,
    MarketSessionStatus,
)
from tradingagents.market_history.config import MarketHistoryConfig
from tradingagents.market_history.models import (
    AdjustmentFactorObservation,
    ProvenanceClass,
    ProviderHistoryBundlePublication,
    RawMarketObservation,
    SnapshotPurpose,
    StrictReplayUnavailable,
    TradingStatus,
    TradingStatusObservation,
    TradingStatusProvenance,
)
from tradingagents.market_history.store import MarketHistoryStore

SYMBOL = "600519.SS"
NOW = datetime(2026, 10, 9, 10, tzinfo=timezone.utc)
DAYS = tuple(item.date() for item in pd.bdate_range(end="2026-10-08", periods=1100))
REAL_BAOSTOCK = snapshot.SNAPSHOT_PROVIDERS["baostock"]
REAL_READY = MarketHistoryStore.mainland_cutover_ready
REAL_DESCRIPTORS = dict(snapshot.SNAPSHOT_PROVIDERS)


@dataclass(frozen=True)
class Candidate:
    frame: pd.DataFrame
    history_bundle: ProviderHistoryBundlePublication
    calendar: MarketSessionCalendarPublication
    current_tradeability: str
    current_status_provenance: TradingStatusProvenance
    latest_traded_close: Decimal
    latest_traded_close_diagnostic: object = None
    carried_suspension_close: object = None


@pytest.fixture
def rig(tmp_path, monkeypatch):
    runtime = config_module.get_config()
    runtime.update(
        market_history_mode="authoritative",
        market_history_database_path=str(tmp_path / "history.sqlite3"),
        market_history_payload_root=str(tmp_path / "payloads"),
        market_history_backup_root=str(tmp_path / "backups"),
        data_usage_mode="personal_research",
        market_data_vendors={"cn_a": {"core_stock_apis": "baostock"}},
    )
    monkeypatch.setattr(config_module, "_config", runtime)
    # Synthetic qualification seam only; never creates compatibility evidence.
    monkeypatch.setattr(MarketHistoryStore, "mainland_cutover_ready", lambda self: True)
    clock = [NOW]

    class FrozenDatetime(datetime):
        @classmethod
        def now(cls, tz=None):
            return clock[0].astimezone(tz) if tz is not None else clock[0].replace(tzinfo=None)

    from tradingagents.dataflows import (
        acquisition,
        baostock_calendar,
        baostock_data,
        tushare_market_history,
    )
    from tradingagents.market_history import (
        bundle,
        calendar,
        calendar_discovery,
        coordinator,
        store as store_module,
    )

    class CalendarRows:
        error_code = "0"
        error_msg = ""
        fields = ["calendar_date", "is_trading_day"]
        def __init__(self, rows=()):
            self.rows = iter(rows)
        def next(self):
            self.row = next(self.rows, None)
            return self.row is not None
        def get_row_data(self):
            return self.row

    class CalendarTransport:
        def login(self):
            return CalendarRows()
        def query_trade_dates(self, *, start_date, end_date):
            return CalendarRows([[str(day.date()), "1" if day.date() in DAYS else "0"]
                for day in pd.date_range(start_date, end_date)])
        def logout(self):
            return CalendarRows()

    acquire_calendar = baostock_calendar.acquire_mainland_session_calendar
    monkeypatch.setattr(calendar_discovery, "acquire_mainland_session_calendar",
        lambda request, **kwargs: acquire_calendar(request, transport_factory=CalendarTransport, **kwargs))

    for module in (
        snapshot,
        current,
        acquisition,
        baostock_data,
        tushare_market_history,
        bundle,
        calendar,
        coordinator,
        store_module,
    ):
        monkeypatch.setattr(module, "datetime", FrozenDatetime)
    from tradingagents.market_history import current_incremental
    monkeypatch.setattr(current_incremental, "datetime", FrozenDatetime)
    monkeypatch.setattr(snapshot, "_coordinator_now", lambda: clock[0])
    monkeypatch.setattr(
        snapshot,
        "_coordinator_sleep",
        lambda seconds: clock.__setitem__(0, clock[0] + timedelta(seconds=seconds)),
    )
    monkeypatch.setattr(current, "_now", lambda: clock[0])
    provider, instrument = current.baostock_history_binding(SYMBOL)
    calls = []
    factor = [Decimal("1")]
    failure = [None]
    omissions = [()]
    factor_anchors = []

    def candidate(start, end, *, omit=()):
        days = [day for day in DAYS if start <= day <= end and day not in omit]
        observations = tuple(
            RawMarketObservation(
                day, Decimal("10"), Decimal("12"), Decimal("9"), Decimal("11"), Decimal("100")
            )
            for day in days
        )
        statuses = tuple(TradingStatusObservation(day, TradingStatus.TRADED) for day in days)
        bundle = ProviderHistoryBundlePublication(
            provider,
            instrument,
            end,
            clock[0],
            clock[0],
            ProvenanceClass.RETROSPECTIVE_BACKFILL,
            json.dumps({"dates": [str(day) for day in days], "factor": str(factor[0])}).encode(),
            observations,
            statuses,
            (AdjustmentFactorObservation(DAYS[0], factor[0]),),
        )
        calendar_days = pd.date_range(start, max(end, DAYS[-1]))
        calendar = MarketSessionCalendarPublication(
            provider=replace(
                provider,
                provider_dataset_id="synthetic-calendar-v1",
                dataset_name="synthetic-calendar-v1",
            ),
            reference_market="mainland-cn",
            timezone_name="Asia/Shanghai",
            observed_at=clock[0],
            provenance_class=ProvenanceClass.RETROSPECTIVE_BACKFILL,
            raw_payload=json.dumps({"start": str(start), "end": str(end)}).encode(),
            sessions=tuple(
                MarketSession(
                    item.date(),
                    MarketSessionStatus.OPEN if item.weekday() < 5 else MarketSessionStatus.CLOSED,
                )
                for item in calendar_days
            ),
        )
        frame = pd.DataFrame(
            [
                {
                    "Date": pd.Timestamp(row.session_date),
                    "Open": float(row.open * factor[0]),
                    "High": float(row.high * factor[0]),
                    "Low": float(row.low * factor[0]),
                    "Close": float(row.close * factor[0]),
                    "Volume": float(row.volume),
                }
                for row in observations
            ]
        )
        return Candidate(
            frame,
            bundle,
            calendar,
            "tradeable",
            TradingStatusProvenance(
                "baostock", provider.provider_dataset_id, days[-1], TradingStatus.TRADED, clock[0]
            ),
            Decimal("11") * factor[0],
        )

    def load(symbol, start, end, *, physical_request=None, adjustment_as_of=None):
        assert symbol == SYMBOL
        clock[0] += timedelta(seconds=1)
        calls.append((start, end))
        factor_anchors.append(adjustment_as_of)
        for operation in ("raw", "status", "factor", "calendar"):

            def request(operation=operation):
                if failure[0] is not None:
                    raise failure[0]
                return {"synthetic": operation, "range": [start, end]}

            physical_request(operation, request)
        return candidate(date.fromisoformat(start), date.fromisoformat(end), omit=omissions[0])

    descriptor = snapshot.SnapshotProvider(
        load,
        "qfq",
        history_load=load,
        uses_typed_physical_requests=True,
        history_binding=lambda symbol: (provider, instrument),
        history_uses_typed_physical_requests=True,
    )
    monkeypatch.setitem(snapshot.SNAPSHOT_PROVIDERS, "baostock", descriptor)
    config = MarketHistoryConfig.from_mapping(runtime)

    def get(start, end, minimum=1):
        return snapshot.get_authoritative_market_snapshot(
            SYMBOL, str(start), str(end), minimum_history_rows=minimum
        )

    def events():
        with MarketHistoryStore.open_provider_request_authority(config) as store:
            return {
                row[0]
                for row in store._connection.execute(
                    "SELECT attempt_event_id FROM provider_request_attempts"
                )
            }

    return {
        "get": get,
        "events": events,
        "candidate": candidate,
        "config": config,
        "calls": calls,
        "factor": factor,
        "failure": failure,
        "provider": provider,
        "instrument": instrument,
        "clock": clock,
        "runtime": runtime,
        "descriptor": descriptor,
        "omissions": omissions,
        "factor_anchors": factor_anchors,
    }


def test_reopen_full_window_increment_and_factor_revision(rig):
    start, end, new_end = DAYS[-261], DAYS[-2], DAYS[-1]
    first = rig["get"](start, end, 200)
    assert len(first.frame) == 260
    assert len(rig["events"]()) == 4
    first_events = rig["events"]()
    second = rig["get"](
        start, end, 200
    )  # stores/coordinators are closed and reconstructed per call
    assert second.history_store_status == "stored"
    assert second.snapshot_id == first.snapshot_id
    assert second.retrieved_at == first.retrieved_at
    assert not second.physical_attempt_events and rig["events"]() == first_events
    assert len(rig["calls"]) == 1
    rig["factor"][0] = Decimal("0.5")
    updated = rig["get"](start, new_end, 200)
    assert len(updated.frame) == 261
    assert rig["calls"][-1] == (str(DAYS[-21]), str(new_end))
    assert updated.frame.Close.tolist() == [5.5] * 261
    assert len(rig["events"]() - first_events) == 4
    assert len(updated.physical_attempt_events) == 4
    assert updated.history_local_rows == 260
    with MarketHistoryStore.open(rig["config"]) as store:
        old = store.read_pinned_snapshot(first.snapshot_id)
        assert old.frame.Close.tolist() == [11.0] * 260
    third_events = rig["events"]()
    assert rig["get"](start, new_end, 200).history_store_status == "stored"
    assert rig["events"]() == third_events


def test_two_distant_gaps_do_not_refetch_middle(rig):
    start, end = DAYS[-261], DAYS[-1]
    gaps = (DAYS[-240], DAYS[-100])
    assert shadow.persist_mainland_history_candidate(
        rig["candidate"](start, end, omit=gaps)
    ).persisted
    result = rig["get"](start, end, 200)
    assert rig["calls"] == [(str(day), str(day)) for day in gaps]
    assert rig["factor_anchors"] == [str(end), str(end)]
    assert len(result.frame) == 261
    assert len(rig["events"]()) == 8


def test_1000_rows_retained_and_small_window_has_own_pin(rig):
    rig["get"](DAYS[-500], DAYS[-1], 200)
    full = rig["get"](DAYS[-1000], DAYS[-1], 200)
    assert len(full.frame) == 1000
    assert rig["calls"][-1] == (str(DAYS[-1000]), str(DAYS[-500] - timedelta(days=1)))
    before = rig["events"]()
    small = rig["get"](DAYS[-220], DAYS[-1], 200)
    assert len(small.frame) == 220 and small.snapshot_id != full.snapshot_id
    assert rig["events"]() == before
    with MarketHistoryStore.open(rig["config"]) as store:
        assert len(store.read_pinned_snapshot(full.snapshot_id).frame) == 1000
        assert len(store.read_pinned_snapshot(small.snapshot_id).frame) == 220


@pytest.mark.parametrize(
    "change",
    ["provider_name", "provider_dataset_id", "adjustment_methodology", "identity_revision"],
)
def test_exact_binding_prevents_foreign_history_hit(rig, change):
    rig["get"](DAYS[-260], DAYS[-1])
    provider, instrument = rig["provider"], rig["instrument"]
    if change == "identity_revision":
        instrument = replace(instrument, identity_revision="other-v2")
    else:
        provider = replace(provider, **{change: "other-v2"})
    with MarketHistoryStore.open(rig["config"]) as store, pytest.raises(KeyError):
        store.latest_qualified_history_bundle(
            SYMBOL, str(DAYS[-1]), provider=provider, instrument=instrument
        )


def test_gate_denial_never_shrinks_online_request(rig, monkeypatch):
    rig["get"](DAYS[-261], DAYS[-2])
    monkeypatch.setattr(MarketHistoryStore, "mainland_cutover_ready", lambda self: False)
    result = rig["get"](DAYS[-261], DAYS[-1])
    assert rig["calls"][-1] == (str(DAYS[-261]), str(DAYS[-1]))
    assert result.history_store_status == "degraded"
    assert result.history_store_diagnostic == "mainland_gate_not_passed"


def test_failed_incremental_publication_keeps_old_pin(rig, monkeypatch):
    first = rig["get"](DAYS[-261], DAYS[-2])

    def fail(*args, **kwargs):
        raise OSError("synthetic publication failure")

    monkeypatch.setattr(MarketHistoryStore, "publish_history_bundle", fail)
    with pytest.raises(NoMarketDataError):
        rig["get"](DAYS[-261], DAYS[-1], 200)
    with MarketHistoryStore.open(rig["config"]) as store:
        assert len(store.read_pinned_snapshot(first.snapshot_id).frame) == 260
        latest = store.latest_qualified_history_bundle(
            SYMBOL, str(DAYS[-1]), provider=rig["provider"], instrument=rig["instrument"]
        )
        assert (
            len(
                store.reconstruct_snapshot(
                    latest.bundle_revision_id,
                    requested_date=DAYS[-1],
                    purpose=SnapshotPurpose.CURRENT_ANALYSIS,
                ).frame
            )
            == 260
        )


def test_rate_limit_cooldown_and_no_coverage(rig):
    rig["failure"][0] = VendorRateLimitError("synthetic", retry_after_seconds=300)
    with pytest.raises(NoMarketDataError):
        rig["get"](DAYS[-260], DAYS[-1])
    before = rig["events"]()
    with pytest.raises(NoMarketDataError):
        rig["get"](DAYS[-260], DAYS[-1])
    assert rig["events"]() == before and len(before) == 1
    with MarketHistoryStore.open(rig["config"]) as store, pytest.raises(KeyError):
        store.latest_qualified_history_bundle(SYMBOL, str(DAYS[-1]))


def test_backfill_remains_ineligible_for_past_strict_replay(rig):
    rig["get"](DAYS[-260], DAYS[-1])
    with MarketHistoryStore.open(rig["config"]) as store:
        bundle = store.latest_qualified_history_bundle(SYMBOL, str(DAYS[-1]))
        with pytest.raises(StrictReplayUnavailable):
            store.reconstruct_snapshot(
                bundle.bundle_revision_id,
                requested_date=DAYS[-1],
                purpose=SnapshotPurpose.STRICT_REPLAY,
                replay_as_of=NOW - timedelta(days=2),
            )


def test_unqualified_tushare_publication_does_not_replace_calendar(rig):
    original = rig["candidate"](DAYS[-260], DAYS[-1])
    assert shadow.persist_mainland_history_candidate(original).persisted
    with MarketHistoryStore.open(rig["config"]) as store:
        before = store.read_current_session_calendar("mainland-cn").calendar_revision_id
    candidate = replace(
        original,
        history_bundle=replace(
            original.history_bundle,
            provider=replace(
                original.history_bundle.provider,
                provider_name="tushare",
                strict_history_qualified=False,
            ),
        ),
    )
    result = shadow.persist_mainland_history_candidate(candidate)
    assert not result.persisted and result.diagnostic == "provider_history_not_qualified"
    with MarketHistoryStore.open(rig["config"]) as store:
        assert store.read_current_session_calendar("mainland-cn").calendar_revision_id == before


@pytest.mark.parametrize("preclose", [False, True])
def test_tushare_explicit_normal_entry_and_qfq(monkeypatch, rig, preclose):
    from tradingagents.dataflows import tushare_market_history as tushare
    from tradingagents.picker.tushare_provider import TushareProvider

    start, latest = DAYS[-260], DAYS[-1]
    end = latest + timedelta(days=1) if preclose else latest
    if preclose:
        rig["clock"][0] = NOW.replace(hour=1)
    query_calls = []

    class Client:
        def query(self, endpoint, **kwargs):
            query_calls.append((endpoint, kwargs))
            assert kwargs.get("ts_code", "600519.SH") == "600519.SH"
            if endpoint == "daily":
                return pd.DataFrame(
                    [
                        {
                            "ts_code": "600519.SH",
                            "trade_date": day.strftime("%Y%m%d"),
                            "open": 10,
                            "high": 12,
                            "low": 9,
                            "close": 11,
                            "vol": 1,
                        }
                        for day in DAYS[-260:]
                    ]
                )
            if endpoint == "adj_factor":
                assert "start_date" not in kwargs
                return pd.DataFrame(
                    [
                        {
                            "ts_code": "600519.SH",
                            "trade_date": day.strftime("%Y%m%d"),
                            "adj_factor": 2 if day == latest else 1,
                        }
                        for day in DAYS
                    ]
                    + (
                        [
                            {
                                "ts_code": "600519.SH",
                                "trade_date": end.strftime("%Y%m%d"),
                                "adj_factor": 3,
                            }
                        ]
                        if preclose
                        else []
                    )
                )
            if endpoint == "suspend_d":
                return pd.DataFrame(
                    columns=["ts_code", "trade_date", "suspend_type", "suspend_timing"]
                )
            return pd.DataFrame(
                [
                    {
                        "exchange": kwargs["exchange"],
                        "cal_date": item.strftime("%Y%m%d"),
                        "is_open": int(item.weekday() < 5),
                    }
                    for item in pd.date_range(start, end)
                ]
            )

    monkeypatch.setattr(
        TushareProvider, "create", lambda *args, **kwargs: TushareProvider(Client())
    )
    rig["runtime"]["market_data_vendors"]["cn_a"]["core_stock_apis"] = "tushare"
    result = rig["get"](start, end, 200)
    assert result.provider == "tushare" and len(result.frame) == 260
    assert result.frame.Close.iloc[0] == 5.5 and result.frame.Close.iloc[-1] == 11
    assert len(result.physical_attempt_events) == 4
    assert [item[0] for item in query_calls] == ["daily", "adj_factor", "suspend_d", "trade_cal"]
    assert "provider_history_not_qualified" in result.history_store_diagnostic
    assert not tushare.history_binding(SYMBOL)[0].strict_history_qualified


def test_unknown_weekend_does_not_consume_correction_sessions():
    from tradingagents.market_history.maintenance import plan_incremental_refresh

    known = tuple(item.date() for item in pd.bdate_range(end="2026-10-09", periods=30))
    plan = plan_incremental_refresh(
        sessions=tuple(MarketSession(day, MarketSessionStatus.OPEN) for day in known),
        as_of_date=date(2026, 10, 12),
        requested_start=known[0],
        retained_session_dates=set(known),
        seeded=True,
    )
    assert set(known[-21:]).issubset(plan.session_dates)
    assert not set(known[:-21]).intersection(plan.session_dates)
    assert {date(2026, 10, 10), date(2026, 10, 11), date(2026, 10, 12)}.issubset(plan.session_dates)


def test_incomplete_refresh_never_claims_coverage(rig):
    first = rig["get"](DAYS[-261], DAYS[-2])
    rig["omissions"][0] = (DAYS[-1],)
    with pytest.raises(
        snapshot.AuthoritativeTradingStatusValidationError,
        match="does not cover the latest applicable mainland session",
    ):
        rig["get"](DAYS[-261], DAYS[-1])
    with MarketHistoryStore.open(rig["config"]) as store:
        assert len(store.read_pinned_snapshot(first.snapshot_id).frame) == 260
        latest = store.latest_qualified_history_bundle(SYMBOL, str(DAYS[-1]))
        row = store._connection.execute(
            "SELECT requested_as_of FROM history_bundle_revisions WHERE bundle_revision_id = ?",
            (latest.bundle_revision_id,),
        ).fetchone()
        assert row[0] == str(DAYS[-2])


def test_tushare_retry_after_survives_normal_entry(monkeypatch, rig):
    from tradingagents.picker.tushare_provider import TushareProvider

    class Throttle(Exception):
        status_code = 429
        retry_after = 300

    calls = []

    class Client:
        def query(self, endpoint, **kwargs):
            calls.append(endpoint)
            raise Throttle("synthetic throttle")

    monkeypatch.setattr(
        TushareProvider, "create", lambda *args, **kwargs: TushareProvider(Client())
    )
    rig["runtime"]["market_data_vendors"]["cn_a"]["core_stock_apis"] = "tushare"
    for _ in range(2):
        with pytest.raises(NoMarketDataError):
            rig["get"](DAYS[-260], DAYS[-1])
    assert calls == ["daily"] and len(rig["events"]()) == 1
    with MarketHistoryStore.open_provider_request_authority(rig["config"]) as store:
        row = store._connection.execute(
            "SELECT retry_after_seconds FROM provider_request_attempts"
        ).fetchone()
        assert row[0] == 300


def test_two_connections_share_one_request_lease(rig):
    from tradingagents.market_history.coordinator import (
        LeaseDisposition,
        ProviderRequestCoordinator,
        RequestPriority,
    )

    with (
        MarketHistoryStore.open_provider_request_authority(rig["config"]) as first,
        MarketHistoryStore.open_provider_request_authority(rig["config"]) as second,
    ):
        leader, follower = ProviderRequestCoordinator(first), ProviderRequestCoordinator(second)
        leader.register_upstream_service("synthetic-service", "synthetic")
        args = {
            "request_key": "synthetic-window",
            "upstream_service_id": "synthetic-service",
            "priority": RequestPriority.INTERACTIVE_MAINLAND,
            "now": NOW,
            "lease_duration": timedelta(minutes=2),
        }
        acquired = leader.acquire(owner_id="one", **args)
        blocked = follower.acquire(owner_id="two", **args)
        assert acquired.disposition is LeaseDisposition.ACQUIRED
        assert blocked.disposition is LeaseDisposition.DUPLICATE_IN_FLIGHT
        leader.release(acquired.lease)
        assert follower.acquire(owner_id="two", **args).disposition is LeaseDisposition.ACQUIRED


def test_existing_publication_cooldown_blocks_polling_then_expires(rig):
    from tradingagents.market_history.maintenance import PublicationState

    rig["get"](DAYS[-261], DAYS[-2])
    before = rig["events"]()
    observed = rig["clock"][0]
    with MarketHistoryStore.open(rig["config"]) as store:
        store.record_publication_watermark(
            instrument_id=rig["instrument"].instrument_id,
            provider_dataset_id=rig["provider"].provider_dataset_id,
            expected_session_date=DAYS[-1], state=PublicationState.NOT_YET_PUBLISHED,
            observed_at=observed, cooldown_until=observed + timedelta(minutes=5),
        )
    for _ in range(2):
        with pytest.raises(NoMarketDataError):
            rig["get"](DAYS[-261], DAYS[-1])
    assert rig["events"]() == before and len(rig["calls"]) == 1
    rig["clock"][0] += timedelta(minutes=6)
    assert len(rig["get"](DAYS[-261], DAYS[-1]).frame) == 261
    assert len(rig["events"]() - before) == 4


@pytest.mark.parametrize("scenario", ["normal", "throttle", "login-throttle", "holiday-suspension"])
def test_baostock_real_adapter_workflow_counts_session_and_cleanup(monkeypatch, rig, scenario):
    from types import SimpleNamespace

    from tradingagents.dataflows import baostock_data as bao

    operations = []
    throttle = scenario == "throttle"
    holiday = DAYS[-2] if scenario == "holiday-suspension" else None

    class Rows:
        error_code = "0"
        error_msg = ""

        def __init__(self, rows):
            self.fields = list(rows[0])
            self.rows = iter(rows)

        def next(self):
            self.row = next(self.rows, None)
            return self.row is not None

        def get_row_data(self):
            return [self.row[key] for key in self.fields]

    def login():
        operations.append("login")
        return SimpleNamespace(error_code="10001005" if scenario == "login-throttle" else "0",
                               error_msg="synthetic capacity")

    def logout():
        operations.append("logout")
        return SimpleNamespace(error_code="0")

    def raw(code, fields, **kwargs):
        operations.append("raw")
        if throttle:
            raise VendorRateLimitError("synthetic", retry_after_seconds=300)
        return Rows(
            [
                {
                    "date": str(day),
                    "code": code,
                    "open": "11" if holiday and day == DAYS[-1] else "10",
                    "high": "11" if holiday and day == DAYS[-1] else "12",
                    "low": "11" if holiday and day == DAYS[-1] else "9",
                    "close": "11",
                    "preclose": "11",
                    "volume": "0" if holiday and day == DAYS[-1] else "100",
                    "amount": "1100",
                    "tradestatus": "0" if holiday and day == DAYS[-1] else "1",
                }
                for day in DAYS[-260:]
                if day != holiday
            ]
        )

    def factors(code, **kwargs):
        operations.append("factor")
        assert kwargs["end_date"] == str(DAYS[-1])
        return Rows([{"dividOperateDate": str(DAYS[0]), "foreAdjustFactor": "1"}])

    def calendar(**kwargs):
        operations.append("calendar")
        return Rows(
            [
                {
                    "calendar_date": item.strftime("%Y-%m-%d"),
                    "is_trading_day": "1" if item.weekday() < 5 and item.date() != holiday else "0",
                }
                for item in pd.date_range(kwargs["start_date"], kwargs["end_date"])
            ]
        )

    for name, function in {
        "login": login,
        "logout": logout,
        "query_history_k_data_plus": raw,
        "query_adjust_factor": factors,
        "query_trade_dates": calendar,
    }.items():
        monkeypatch.setattr(bao.bs, name, function)
    monkeypatch.setitem(snapshot.SNAPSHOT_PROVIDERS, "baostock", REAL_BAOSTOCK)
    if throttle or scenario == "login-throttle":
        with pytest.raises(NoMarketDataError):
            rig["get"](DAYS[-260], DAYS[-1])
        assert operations == (["login", "raw", "logout"] if throttle else ["login"])
        before = rig["events"]()
        with pytest.raises(NoMarketDataError):
            rig["get"](DAYS[-260], DAYS[-1])
        assert rig["events"]() == before and len(before) == (3 if throttle else 1)
    else:
        result = rig["get"](DAYS[-260], DAYS[-1])
        assert operations == ["login", "raw", "factor", "calendar", "logout"]
        assert (
            len(result.frame) == (259 if holiday else 260)
            and len(result.physical_attempt_events) == 5
        )
        if holiday:
            assert result.current_tradeability == "suspended" and result.frame.Volume.iloc[-1] == 0
            assert result.carried_suspension_close == Decimal("11")
        rig["get"](DAYS[-260], DAYS[-1])
        assert len(operations) == 5


# The compatibility matrix exercises the codec directly BEFORE installing its
# manifest. It is not the public-entry acceptance, which runs below without a
# gate mock. No qualification record is inserted in either phase.
CURRENT_MATRIX = {}
CURRENT_MATRIX_DETAILS = {}


@pytest.fixture
def current_rig(rig, monkeypatch):
    from tradingagents.market_history import current_cache as cache
    from tradingagents.picker.tushare_provider import TushareProvider
    monkeypatch.setattr(MarketHistoryStore, "mainland_cutover_ready", REAL_READY)
    rig["runtime"]["market_history_mode"] = "current"
    rig["config"] = MarketHistoryConfig.from_mapping(rig["runtime"])
    monkeypatch.setattr(cache, "_now", lambda: rig["clock"][0])
    query_calls = []
    omitted = []
    multiplier = [1]
    start, end = DAYS[-25], DAYS[-1]

    class Client:
        def query(self, endpoint, **kwargs):
            query_calls.append((endpoint, kwargs))
            code = kwargs.get("ts_code", "600519.SH")
            lower = datetime.strptime(kwargs.get("start_date", "19900101"), "%Y%m%d").date()
            upper = datetime.strptime(kwargs["end_date"], "%Y%m%d").date()
            days = [day for day in DAYS if lower <= day <= upper]
            if endpoint == "daily":
                return pd.DataFrame([{"ts_code": code, "trade_date": day.strftime("%Y%m%d"),
                    "open": 10, "high": 12, "low": 9, "close": 11, "vol": 1} for day in days if day not in omitted])
            if endpoint == "adj_factor":
                return pd.DataFrame([{"ts_code": code, "trade_date": day.strftime("%Y%m%d"),
                    "adj_factor": multiplier[0] if day == end else 1} for day in days])
            if endpoint == "suspend_d":
                return pd.DataFrame(columns=["ts_code", "trade_date", "suspend_type", "suspend_timing"])
            return pd.DataFrame([{"exchange": kwargs["exchange"], "cal_date": day.strftime("%Y%m%d"),
                "is_open": int(day.date() in DAYS)} for day in pd.date_range(lower, upper)])

    monkeypatch.setattr(TushareProvider, "create", lambda *args, **kwargs: TushareProvider(Client()))
    for name, descriptor in REAL_DESCRIPTORS.items():
        monkeypatch.setitem(snapshot.SNAPSHOT_PROVIDERS, name, descriptor)
    from tradingagents.dataflows import stockstats_utils
    class SyntheticYahooSession:
        def get(self, *args, **kwargs):
            raise AssertionError("synthetic Yahoo must use its injected transport")
        post = get
    guard = stockstats_utils._YahooSessionAttemptGuard(SyntheticYahooSession())
    monkeypatch.setattr(stockstats_utils, "_yahoo_session_guard", lambda: guard)
    monkeypatch.setattr(stockstats_utils, "_activate_yahoo_session_guard", lambda guard: None)
    rig["runtime"]["market_data_vendors"]["cn_a"]["core_stock_apis"] = "tushare"
    rig.update(start=start, end=end, queries=query_calls, omitted=omitted, multiplier=multiplier)
    return rig


def current_roundtrip(rig, live, *, start=None, end=None):
    from tradingagents.market_history import current_cache as cache
    start, end = str(start or rig["start"]), str(end or rig["end"])
    descriptor = snapshot.SNAPSHOT_PROVIDERS[live.provider]
    with MarketHistoryStore.open(rig["config"]) as store:
        provider, instrument = cache.binding(live.symbol, live.provider, descriptor, store=store)
        assert not store.mainland_cutover_ready()
        loaded = store.load_current_provider_frame(instrument.canonical_symbol, start, end,
            minimum_history_rows=21, provider=provider, instrument=instrument,
            adjustment_basis=descriptor.adjustment_basis)
    # Decode only after that SQLite connection has closed.
    restored = cache.decode(loaded, descriptor, live.symbol, start, end, 21)
    from tradingagents.strategy_registry import build_market_return_fact
    left = build_market_return_fact(live, live.source_artifact).model_dump(mode="json")
    right = build_market_return_fact(restored, restored.source_artifact).model_dump(mode="json")
    assert left == right
    assert live.snapshot_id == restored.snapshot_id and live.snapshot_manifest_json == restored.snapshot_manifest_json
    assert live.frame_sha256 == restored.frame_sha256 and live.retrieved_at == restored.retrieved_at
    assert live.acquisition_outcomes == restored.acquisition_outcomes
    assert not restored.physical_attempt_events and restored.history_local_rows == len(live.frame)
    return restored, loaded, {"live": cache.digest(left), "restored": cache.digest(right),
        "lineage": cache.digest(left["calculation_lineage"]), "snapshot": restored.snapshot_id,
        "provider": live.provider, "symbol": live.symbol, "rows": len(live.frame)}


@pytest.mark.parametrize("scenario", [
    "shanghai_primary", "shenzhen_primary", "baostock_fallback", "yahoo_fallback",
    "confirmed_suspension", "representation_noise", "in_range_gap", "factor_revision",
    "rate_limit_fallback", "store_corruption_degradation", "concurrent_write", "crash_recovery",
])
def test_current_cache_real_compatibility_matrix(current_rig, monkeypatch, scenario):
    from tradingagents.evidence import SourceArtifact
    from tradingagents.market_history import current_cache as cache
    rig = current_rig
    start, end = rig["start"], rig["end"]
    symbol = "000001.SZ" if scenario == "shenzhen_primary" else SYMBOL
    faults = []
    if scenario == "representation_noise":
        text = "Date,Open,High,Low,Close,Volume\n2026-10-08,10,12,9,11,100\n"
        legacy = shadow.persist_mainland_provider_frame(symbol=symbol, provider="akshare",
            adjustment_basis="qfq", requested_start=str(end), requested_end=str(end),
            effective_trading_date=str(end), retrieved_at=NOW.isoformat(),
            snapshot_id="synthetic-legacy-frame", frame_digest=cache.sha256(text.encode()).hexdigest(),
            history_rows=1, normalized_frame_text=text)
        assert legacy.persisted
    if scenario in {"baostock_fallback", "yahoo_fallback", "rate_limit_fallback", "representation_noise", "confirmed_suspension"}:
        target = "yfinance" if scenario == "yahoo_fallback" else "baostock" if scenario != "representation_noise" else "akshare"
        rig["runtime"]["market_data_vendors"]["cn_a"]["core_stock_apis"] = ("akshare," + target if target != "akshare" else target)
        calendar = rig["candidate"](start, end).calendar
        with MarketHistoryStore.open(rig["config"]) as store:
            store.publish_session_calendar(calendar)
        def unavailable(*args, physical_request=None, **kwargs):
            def fail():
                if scenario == "rate_limit_fallback":
                    raise VendorRateLimitError("synthetic current cache throttle")
                raise NoMarketDataError("synthetic unavailable")
            return physical_request("synthetic", fail)
        monkeypatch.setitem(snapshot.SNAPSHOT_PROVIDERS, "akshare",
            replace(REAL_DESCRIPTORS["akshare"], load=unavailable))
        def load(_symbol, first, last, *, physical_request=None, **kwargs):
            def fetch():
                frame = rig["candidate"](date.fromisoformat(first), date.fromisoformat(last)).frame
                if scenario == "representation_noise":
                    frame = frame.iloc[::-1].reset_index(drop=True)
                    for column in ("Open", "High", "Low", "Close", "Volume"):
                        frame[column] = frame[column].map(lambda value: f"{value:.8f}")
                    return frame
                if scenario == "confirmed_suspension":
                    from tradingagents.dataflows.baostock_data import (
                        BaoStockCurrentSnapshotCandidate,
                    )
                    frame.loc[frame.index[-1], ["Open", "High", "Low", "Close", "Volume"]] = [11, 11, 11, 11, 0]
                    artifact = SourceArtifact(artifact_sha256=cache.sha256(b"synthetic suspension").hexdigest(),
                        source_ref="synthetic-status", tool_call_id="synthetic-status", tool_name="synthetic", raw_text="synthetic suspension")
                    statuses = tuple(TradingStatusObservation(day,
                        TradingStatus.SUSPENDED if day == end else TradingStatus.TRADED,
                        Decimal("11") if day == end else None, Decimal("0") if day == end else None)
                        for day in DAYS[-25:])
                    return BaoStockCurrentSnapshotCandidate(frame, statuses, "suspended",
                        TradingStatusProvenance("baostock", "synthetic-status", end, TradingStatus.SUSPENDED,
                            rig["clock"][0], "artifact:sha256:" + artifact.artifact_sha256),
                        artifact, Decimal("11"), None, Decimal("11"))
                return frame
            if physical_request and scenario == "confirmed_suspension":
                physical_request("synthetic", lambda: {"synthetic_status": "suspended"})
                return fetch()
            return physical_request("synthetic", fetch) if physical_request else fetch()
        monkeypatch.setitem(snapshot.SNAPSHOT_PROVIDERS, target,
            replace(REAL_DESCRIPTORS[target], load=load, history_load=None, uses_typed_physical_requests=target != "yfinance"))
    if scenario == "in_range_gap":
        rig["omitted"].append(DAYS[-10])
        with pytest.raises(NoMarketDataError):
            rig["get"](start, end, 21)
        faults.append("gap-rejected-before-publication")
        rig["omitted"].clear()
        rig["clock"][0] += timedelta(minutes=6)
    live = snapshot.get_authoritative_market_snapshot(symbol, str(start), str(end), minimum_history_rows=21)
    restored, loaded, proof = current_roundtrip(rig, live)
    if scenario == "representation_noise":
        with MarketHistoryStore.open(rig["config"]) as store:
            assert loaded.stored.instrument_id == store.get_provider_frame_revision(legacy.frame_revision_id).instrument_id
        monkeypatch.setattr(cache, "compatibility_diagnostic", lambda: None)
        cached, diagnostic = cache.try_read(symbol, str(start), str(end), ["akshare"],
                                            snapshot.SNAPSHOT_PROVIDERS, 21)
        assert diagnostic is None and cached.snapshot_id == live.snapshot_id
        faults.append("legacy-shadow-identity-retained;adjusted-frame-readback")
    if scenario in {"baostock_fallback", "yahoo_fallback", "rate_limit_fallback"}:
        assert live.acquisition_outcomes[0].provider == "akshare" and live.acquisition_outcomes[0].outcome == "unavailable"
        faults.append([outcome.model_dump(mode="json") for outcome in live.acquisition_outcomes])
    if scenario == "confirmed_suspension":
        assert restored.current_tradeability == "suspended" and restored.status_artifact == live.status_artifact
    if scenario == "factor_revision":
        rig["multiplier"][0] = 2
        rig["clock"][0] += timedelta(days=2)
        live2 = rig["get"](start, end, 21)
        assert live2.frame_sha256 != live.frame_sha256
        _, _, second = current_roundtrip(rig, live2)
        faults.append(second)
    if scenario == "store_corruption_degradation":
        with MarketHistoryStore.open(rig["config"]) as store:
            store._connection.execute("UPDATE provider_frame_revisions SET frame_digest = ? WHERE frame_revision_id = ?", ("0"*64, loaded.stored.frame_revision_id))
            from tradingagents.market_history.store import MarketHistoryCorruptionError
            with pytest.raises(MarketHistoryCorruptionError):
                store.load_provider_frame_revision(loaded.stored.frame_revision_id)
        faults.append("corruption-rejected")
    if scenario == "concurrent_write":
        from tradingagents.market_history.store import ProviderFramePublication
        meta = loaded.metadata
        publication = ProviderFramePublication(**meta["provider"], **{
            "instrument_id": meta["instrument"]["instrument_id"],
            "canonical_symbol": meta["instrument"]["canonical_symbol"],
            "reference_market": meta["instrument"]["reference_market"],
            "instrument_kind": meta["instrument"]["instrument_kind"],
            "currency": meta["instrument"]["currency"],
            "identity_revision": meta["instrument"]["identity_revision"]},
            snapshot_id=loaded.stored.snapshot_id, requested_start=str(start), requested_end=str(end),
            effective_trading_date=live.effective_trading_date, adjustment_basis=live.adjustment_basis,
            frame_digest=live.frame_sha256, history_rows=len(live.frame), retrieved_at=live.retrieved_at,
            normalized_frame=live.source_artifact.raw_text.encode("utf-8"), snapshot_metadata=meta)
        with MarketHistoryStore.open(rig["config"]) as one, MarketHistoryStore.open(rig["config"]) as two:
            import sqlite3
            two._connection.execute("PRAGMA busy_timeout = 0")
            one._connection.execute("BEGIN IMMEDIATE")
            with pytest.raises(sqlite3.OperationalError, match="locked"):
                two.publish_provider_frame(publication)
            one._connection.rollback()
            one.publish_provider_frame(publication)
            two.publish_provider_frame(publication)
            assert one.load_provider_frame_revision(loaded.stored.frame_revision_id).metadata == two.load_provider_frame_revision(loaded.stored.frame_revision_id).metadata
            assert one._connection.execute("SELECT COUNT(*) FROM provider_frame_revisions").fetchone()[0] == 1
        faults.append("overlapping-writer-lock-rejected;two-open-connections-idempotent-writes")
    if scenario == "crash_recovery":
        with MarketHistoryStore.open(rig["config"]) as store:
            store._connection.execute("BEGIN IMMEDIATE")
            store._connection.execute("DELETE FROM provider_frame_revisions WHERE frame_revision_id = ?", (loaded.stored.frame_revision_id,))
            # Closing abandons an uncommitted transaction; no subprocess/native
            # crash is launched or claimed by this bounded recovery scenario.
        _, _, reopened = current_roundtrip(rig, live)
        faults.append({"connection_close_abandons_uncommitted_delete": reopened})
    proof["faults"] = faults
    CURRENT_MATRIX_DETAILS[scenario] = proof
    CURRENT_MATRIX[scenario] = {"passed": True, "evidence_sha256": cache.digest(proof)}


@pytest.fixture
def validated_manifest(tmp_path, monkeypatch):
    from tradingagents.market_history import current_cache as cache
    assert set(CURRENT_MATRIX) == set(cache.SCENARIOS), "real matrix must pass before public-entry acceptance"
    report = {"matrix": CURRENT_MATRIX, "details": CURRENT_MATRIX_DETAILS,
              "verification_sources": {name: cache.source_fingerprints()[name] for name in cache.VERIFICATION_FILES},
              "scope": "isolated-synthetic-current-cache-only", "mechanism_phase": "direct-codec-before-manifest"}
    path = tmp_path / "current-cache-compatibility-manifest.json"
    evidence_path = tmp_path / "current-cache-matrix-evidence.json"
    evidence_path.write_text(cache.canonical(report), encoding="utf-8")
    manifest = {"contract": cache.MANIFEST_CONTRACT, "mechanism": cache.CONTRACT,
        "sources": cache.source_fingerprints(), "matrix": CURRENT_MATRIX,
        "details": CURRENT_MATRIX_DETAILS,
        "matrix_sha256": cache.digest(CURRENT_MATRIX),
        "evidence": {"path": evidence_path.name, "sha256": cache.sha256(evidence_path.read_bytes()).hexdigest(),
                     "scope": "isolated-synthetic-current-cache-only"}}
    path.write_text(cache.canonical(manifest), encoding="utf-8")
    monkeypatch.setattr(cache, "MANIFEST_PATH", path)
    assert cache.compatibility_diagnostic() is None
    return path


def test_current_public_entry_persistent_tushare_hit(current_rig, validated_manifest):
    rig = current_rig
    live = rig["get"](rig["start"], rig["end"], 21)
    before = rig["events"]()
    assert len(rig["queries"]) == 4
    cached = rig["get"](rig["start"], rig["end"], 21)
    assert cached.history_store_status == "current_only_cached", cached.history_store_diagnostic
    assert len(rig["queries"]) == 4 and rig["events"]() == before
    assert cached.snapshot_id == live.snapshot_id and cached.retrieved_at == live.retrieved_at
    assert cached.history_local_rows == 25 and not cached.physical_attempt_events
    from tradingagents.evidence import build_evidence_state
    evidence = build_evidence_state(symbol=SYMBOL, identity={}, snapshot=cached)
    assert evidence.market_snapshot.history_store_status == "stored"
    assert evidence.market_snapshot.snapshot_id == live.snapshot_id
    assert evidence.market_snapshot.physical_attempt_count == 0
    with MarketHistoryStore.open(rig["config"]) as store:
        assert not store.mainland_cutover_ready()
        with pytest.raises(StrictReplayUnavailable):
            store.read_pinned_snapshot(live.snapshot_id)
    print("CURRENT_CACHE_MANIFEST=" + str(validated_manifest))


@pytest.mark.parametrize("reuse", [True, False], ids=["cached", "refreshed"])
def test_current_cached_snapshot_survives_analyst_audit_and_report(
    current_rig, validated_manifest, monkeypatch, tmp_path, reuse,
):
    """Real cache/evidence/submission/rendering; only acquisition/model IO is synthetic.

    Catches the runtime status rejected after a cache hit, loss of current-only
    provenance in checkpoint/audit/report, and accidental cache-hit acquisition.
    Does not exercise a model transport, graph scheduling or native report IO.
    """
    from langchain_core.messages import AIMessage
    from pydantic import ValidationError

    from tradingagents.agents.analysts import submission
    from tradingagents.evidence import (
        AnalysisDiagnosticCode,
        AnalysisOutcome,
        AnalysisOutcomeReason,
        EvidenceReadiness,
        EvidenceSource,
        EvidenceState,
        EvidenceStatus,
        build_evidence_state,
        render_analysis_outcome,
    )
    from tradingagents.evidence_artifacts import (
        AuditEvidenceProjection,
        persist_source_artifacts,
        project_evidence_for_audit,
    )

    rig = current_rig
    first = rig["get"](rig["start"], rig["end"], 21)
    before = rig["events"]()
    cached = rig["get"](rig["start"], rig["end"], 21) if reuse else first
    expected_status = "current_only_cached" if reuse else "current_only_refreshed"
    assert cached.history_store_status == expected_status
    assert cached.snapshot_id == first.snapshot_id
    assert rig["events"]() == before and len(rig["queries"]) == 4
    evidence = build_evidence_state(symbol=SYMBOL, identity={}, snapshot=cached)
    # The production graph's active-snapshot lookup receives the exact snapshot
    # returned above, not a fabricated typed evidence object.
    monkeypatch.setattr(submission, "get_active_authoritative_market_snapshot", lambda *_: cached)
    state = {
        "company_of_interest": SYMBOL,
        "trade_date": str(rig["end"]),
        "messages": [],
        "evidence_state": evidence.model_dump(mode="json"),
    }
    result = submission.AnalystSubmissionResult(
        message=AIMessage(content="合成市场分析"),
        report="合成市场分析",
        submission_source=EvidenceSource(
            source_id="analyst_submission:market", status=EvidenceStatus.AVAILABLE, required=True,
        ),
    )
    update = submission.build_analyst_update(state, result, "market_report")
    from tradingagents.decision_audit import prepare_decision_audit
    from tradingagents.report_presentation import localize_report
    from tradingagents.reporting import render_analysis_outcome_report
    from tradingagents.terminal_contract import build_terminal_contract

    restored = EvidenceState.model_validate_json(json.dumps(update["evidence_state"]))
    observed = restored.market_snapshot
    assert observed.history_store_status == "stored"
    assert f"runtime_history_store_status={expected_status}" in observed.history_store_diagnostic
    assert "strict_history_qualified=false" in observed.history_store_diagnostic
    assert cached.history_store_diagnostic in observed.history_store_diagnostic
    assert observed.snapshot_manifest_json == cached.snapshot_manifest_json
    assert observed.retrieved_at == cached.retrieved_at
    assert observed.physical_attempt_count == (0 if reuse else 4)
    assert observed.model_dump(mode="json") == evidence.market_snapshot.model_dump(mode="json")
    manifest = persist_source_artifacts(restored, tmp_path / "audit")
    audit = project_evidence_for_audit(restored, manifest)
    audit = AuditEvidenceProjection.model_validate_json(audit.model_dump_json())
    assert audit.market_snapshot.history_store_status == "stored"
    assert audit.market_snapshot.history_store_diagnostic == observed.history_store_diagnostic
    assert audit.market_snapshot.snapshot_manifest_json == cached.snapshot_manifest_json
    outcome = AnalysisOutcome(
        readiness=EvidenceReadiness.INSUFFICIENT,
        reason=AnalysisOutcomeReason.PREFLIGHT_BLOCKED,
        diagnostic_codes=(AnalysisDiagnosticCode.IDENTITY_UNAVAILABLE,),
    )
    terminal_state = {
        **state, **update,
        "analysis_outcome_contract": outcome.model_dump(mode="json"),
        "analysis_outcome": render_analysis_outcome(outcome),
    }
    terminal = build_terminal_contract(terminal_state)
    final_audit = prepare_decision_audit(terminal_state, tmp_path / "final-audit", config=rig["runtime"])
    assert final_audit["evidence_state"]["market_snapshot"]["history_store_diagnostic"] == observed.history_store_diagnostic
    report = render_analysis_outcome_report(outcome, terminal, restored)
    chinese = localize_report(report, "Chinese")
    assert expected_status in report and "strict_history_qualified=false" in report
    assert "仅供当前分析复用" in chinese
    assert terminal.terminal_outcome_kind.value == "analysis_outcome"
    assert rig["events"]() == before and len(rig["queries"]) == 4
    with MarketHistoryStore.open(rig["config"]) as store, pytest.raises(StrictReplayUnavailable):
        store.read_pinned_snapshot(cached.snapshot_id)
    # Reintroduce the original broken projection at the production call site.
    # Assert the precise schema/input failure, so a setup error cannot count as
    # a successful sensitivity check. No model, acquisition or writer is called.
    with monkeypatch.context() as mutation:
        mutation.setattr(submission, "project_history_store_access", lambda status, diagnostic: (status, diagnostic))
        with pytest.raises(ValidationError) as error:
            submission.build_analyst_update(state, result, "market_report")
        assert [(item["loc"], item["type"], item["input"]) for item in error.value.errors()] == [
            (("history_store_status",), "literal_error", expected_status),
        ]
    print(f"CACHED_STATUS_MUTATION_DETECTED={expected_status}")


@pytest.mark.parametrize("status", ["live", "stored", "degraded", "refreshed", "unknown-state"])
def test_history_access_evidence_boundary_preserves_legacy_and_rejects_unknown(
    current_rig, validated_manifest, monkeypatch, status,
):
    from langchain_core.messages import AIMessage
    from pydantic import ValidationError

    from tradingagents.agents.analysts import submission
    from tradingagents.evidence import (
        EvidenceSource,
        EvidenceState,
        EvidenceStatus,
        build_evidence_state,
    )
    from tradingagents.evidence_artifacts import (
        persist_source_artifacts,
        project_evidence_for_audit,
    )

    rig = current_rig
    acquired = rig["get"](rig["start"], rig["end"], 21)
    value = replace(acquired, history_store_status=status, history_store_diagnostic="original-diagnostic")
    monkeypatch.setattr(submission, "get_active_authoritative_market_snapshot", lambda *_: value)
    state = {"company_of_interest": SYMBOL, "trade_date": str(rig["end"]), "messages": []}
    result = submission.AnalystSubmissionResult(
        message=AIMessage(content="合成市场分析"), report="合成市场分析",
        submission_source=EvidenceSource(source_id="analyst_submission:market", status=EvidenceStatus.AVAILABLE, required=True),
    )
    if status == "unknown-state":
        with pytest.raises(ValidationError, match="history_store_status"):
            build_evidence_state(symbol=SYMBOL, identity={}, snapshot=value)
        with pytest.raises(ValidationError, match="history_store_status"):
            submission.build_analyst_update(state, result, "market_report")
        return
    expected = "stored" if status == "refreshed" else status
    initial = build_evidence_state(symbol=SYMBOL, identity={}, snapshot=value)
    state["evidence_state"] = initial.model_dump(mode="json")
    updated = submission.build_analyst_update(state, result, "market_report")
    evidence = EvidenceState.model_validate_json(json.dumps(updated["evidence_state"]))
    assert evidence.market_snapshot.history_store_status == expected
    assert evidence.market_snapshot == initial.market_snapshot
    assert "original-diagnostic" in evidence.market_snapshot.history_store_diagnostic
    manifest = persist_source_artifacts(evidence, rig["config"].payload_root.parent / "legacy-audit")
    assert project_evidence_for_audit(evidence, manifest).market_snapshot.history_store_status == expected


@pytest.mark.parametrize("change", ["expired", "window", "version", "basis", "provider", "corruption",
                                    "unknown_instrument_id", "instrument_metadata"])
def test_adjusted_frame_codec_rejects_incompatible_evidence(current_rig, validated_manifest, change):
    # The codec remains an exact-window evidence contract. Tushare's normal
    # entry now uses raw history; its incremental refresh is covered separately.
    from tradingagents.market_history import current_cache as cache
    rig = current_rig
    live = rig["get"](rig["start"], rig["end"], 21)
    _, loaded, _ = current_roundtrip(rig, live)
    descriptor = snapshot.SNAPSHOT_PROVIDERS["tushare"]
    start, end = rig["start"], rig["end"]
    if change == "expired":
        rig["clock"][0] += timedelta(days=2)
    elif change == "window":
        start = DAYS[-26]
    elif change in {"version", "basis"}:
        descriptor = replace(descriptor,
            **({"current_cache_version": "different-v2"} if change == "version" else {"adjustment_basis": "raw"}))
    elif change == "provider":
        loaded.metadata["provider"]["adjustment_methodology"] = "poison"
    elif change in {"unknown_instrument_id", "instrument_metadata"}:
        if change == "unknown_instrument_id":
            loaded.metadata["instrument"]["instrument_id"] = "unknown-instrument"
        else:
            loaded.metadata["instrument"]["currency"] = "USD"
        loaded = replace(loaded, stored=replace(loaded.stored,
            instrument_id=loaded.metadata["instrument"]["instrument_id"],
            snapshot_id="current-cache-record=sha256:" + cache.digest(loaded.metadata)))
    else:
        loaded.frame.loc[loaded.frame.index[0], "Close"] = 999
    with pytest.raises(ValueError):
        cache.decode(loaded, descriptor, SYMBOL, str(start), str(end), 21)
    assert len(rig["queries"]) == 4


@pytest.mark.parametrize("change", ["missing_manifest", "source_mismatch", "shadow", "disabled"])
def test_current_gate_or_disabled_mode_uses_independent_live_acquisition(current_rig, validated_manifest, monkeypatch, change):
    from tradingagents.market_history import current_cache as cache
    rig = current_rig
    start, end = rig["start"], rig["end"]
    rig["get"](start, end, 21)
    if change == "missing_manifest":
        monkeypatch.setattr(cache, "MANIFEST_PATH", validated_manifest.with_name("absent.json"))
    elif change == "source_mismatch":
        manifest = json.loads(validated_manifest.read_text(encoding="utf-8"))
        manifest["sources"][cache.SOURCE_FILES[0]] = "0"*64
        validated_manifest.write_text(json.dumps(manifest), encoding="utf-8")
    else:
        rig["runtime"]["market_history_mode"] = change
    result = rig["get"](start, end, 21)
    assert len(rig["queries"]) == 8 and result.history_store_status != "current_only_cached"
    if change in {"missing_manifest", "source_mismatch"}:
        assert "current_cache_gate_denied" in result.history_store_diagnostic


def test_closed_start_extension_discovers_calendar_without_price_request(rig, monkeypatch):
    from tradingagents.dataflows import baostock_calendar
    from tradingagents.market_history import calendar_discovery
    start, end = date(2026, 9, 28), date(2026, 10, 2)
    first = rig["get"](start, end)
    before_events, before_prices = rig["events"](), len(rig["calls"])
    operations = []

    class Response:
        error_code = "0"
        error_msg = ""
        fields = ["calendar_date", "is_trading_day"]
        def __init__(self, rows=()):
            self.rows = iter(rows)
        def next(self):
            self.row = next(self.rows, None)
            return self.row is not None
        def get_row_data(self):
            return self.row

    class CalendarTransport:
        def login(self):
            operations.append("login")
            return Response()
        def query_trade_dates(self, *, start_date, end_date):
            operations.append((start_date, end_date))
            assert start_date == end_date == "2026-09-27"
            return Response([[start_date, "0"]])
        def logout(self):
            operations.append("logout")
            return Response()

    acquire = baostock_calendar.acquire_mainland_session_calendar
    monkeypatch.setattr(calendar_discovery, "acquire_mainland_session_calendar",
        lambda request, **kwargs: acquire(request, transport_factory=CalendarTransport, **kwargs))
    result = rig["get"](date(2026, 9, 27), end)
    assert len(rig["calls"]) == before_prices
    assert operations == ["login", ("2026-09-27", "2026-09-27"), "logout"]
    assert len(rig["events"]() - before_events) == 3 and len(result.physical_attempt_events) == 3
    assert result.history_requested_ranges == () and len(result.frame) == 5
    assert result.frame_sha256 == first.frame_sha256 and result.history_local_rows == 5
    with MarketHistoryStore.open(rig["config"]) as store:
        view = calendar_discovery.read_mainland_calendar_window(store, date(2026, 9, 27), end)
        assert len(view.source_calendar_revision_ids) == 2
        assert view.sessions[0].status is MarketSessionStatus.CLOSED
    again = rig["get"](date(2026, 9, 27), end)
    assert len(operations) == 3 and len(rig["calls"]) == before_prices
    assert not again.physical_attempt_events


@pytest.mark.parametrize("provider_name", ["akshare", "baostock", "yfinance"])
def test_current_public_provider_frame_reuse(current_rig, validated_manifest, monkeypatch, provider_name):
    rig = current_rig
    with MarketHistoryStore.open(rig["config"]) as store:
        store.publish_session_calendar(rig["candidate"](rig["start"], rig["end"]).calendar)
    calls = []
    def load(symbol, first, last, *, physical_request, **kwargs):
        def fetch():
            calls.append(provider_name)
            return rig["candidate"](date.fromisoformat(first), date.fromisoformat(last)).frame
        return physical_request("synthetic-current-frame", fetch)
    monkeypatch.setitem(snapshot.SNAPSHOT_PROVIDERS, provider_name,
        replace(REAL_DESCRIPTORS[provider_name], load=load, history_load=None, uses_typed_physical_requests=True))
    rig["runtime"]["market_data_vendors"]["cn_a"]["core_stock_apis"] = provider_name
    first = rig["get"](rig["start"], rig["end"], 21)
    before = rig["events"]()
    second = rig["get"](rig["start"], rig["end"], 21)
    assert second.history_store_status == "current_only_cached"
    assert calls == [provider_name] and rig["events"]() == before
    assert first.snapshot_id == second.snapshot_id


def test_current_real_baostock_adapter_uses_full_workflow(current_rig, validated_manifest, monkeypatch):
    current_rig["runtime"]["market_data_vendors"]["cn_a"]["core_stock_apis"] = "baostock"
    test_baostock_real_adapter_workflow_counts_session_and_cleanup(monkeypatch, current_rig, "normal")


def test_closed_end_extension_does_not_trigger_correction_overlap(rig):
    start, end = date(2026, 9, 28), date(2026, 10, 2)
    candidate = rig["candidate"](start, end)
    candidate = replace(candidate, calendar=replace(candidate.calendar,
        sessions=tuple(item for item in candidate.calendar.sessions if item.session_date <= end)))
    assert shadow.persist_mainland_history_candidate(candidate).persisted
    first = rig["get"](start, end)
    before = rig["events"]()
    result = rig["get"](start, date(2026, 10, 4))
    assert rig["calls"] == [] and result.history_requested_ranges == ()
    assert len(rig["events"]() - before) == 3 and len(result.physical_attempt_events) == 3
    assert result.frame_sha256 == first.frame_sha256 and result.history_local_rows == 5


def test_explicit_local_first_retains_fallback_until_mode_rollback(current_rig, validated_manifest, monkeypatch):
    rig = current_rig
    calls = []
    healthy = [False]
    with MarketHistoryStore.open(rig["config"]) as store:
        store.publish_session_calendar(rig["candidate"](rig["start"], rig["end"]).calendar)
    def loader(name):
        def load(symbol, start, end, *, physical_request, **kwargs):
            def request():
                calls.append(name)
                if name == "akshare" and not healthy[0]:
                    raise NoMarketDataError("synthetic primary unavailable")
                return rig["candidate"](date.fromisoformat(start), date.fromisoformat(end)).frame
            return physical_request("synthetic-current", request)
        return load
    for name in ("akshare", "yfinance"):
        monkeypatch.setitem(snapshot.SNAPSHOT_PROVIDERS, name, replace(REAL_DESCRIPTORS[name],
            load=loader(name), history_load=None, uses_typed_physical_requests=True))
    rig["runtime"]["market_data_vendors"]["cn_a"]["core_stock_apis"] = "akshare,yfinance"
    first = rig["get"](rig["start"], rig["end"], 21)
    assert first.provider == "yfinance" and calls == ["akshare", "yfinance"]
    healthy[0] = True
    second = rig["get"](rig["start"], rig["end"], 21)
    assert second.provider == "yfinance" and calls == ["akshare", "yfinance"]
    assert second.acquisition_outcomes == first.acquisition_outcomes
    assert "retained_acquisition_evidence" in second.history_store_diagnostic
    rig["runtime"]["market_history_mode"] = "shadow"
    third = rig["get"](rig["start"], rig["end"], 21)
    assert third.provider == "akshare" and calls == ["akshare", "yfinance", "akshare"]

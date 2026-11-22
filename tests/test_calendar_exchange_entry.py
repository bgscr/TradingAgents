"""Synthetic security -> bank overlay -> calendar -> real entry precheck regression."""
import json
from datetime import date, timedelta

import pytest

from tests.test_bank_identity_acquisition import authority
from tests.test_bank_identity_snapshot import (
    NOW,
    exact_snapshot,
    save_exact_snapshot,
    source_layout,
)
from tests.test_bounded_mainland_entry import entry
from tests.test_market_history_persistent_reuse import validated_manifest  # noqa: F401
from tests.test_tushare_sse_prepared_calendar import publication
from tradingagents import statement_acceptance
from tradingagents.dataflows import bank_identity_snapshot as bank, config as config_module
from tradingagents.default_config import DEFAULT_CONFIG
from tradingagents.market_history.config import MarketHistoryConfig
from tradingagents.market_history.store import MarketHistoryStore

START, END = date(2021, 11, 1), date(2026, 11, 1)

def prepare_case(tmp_path, monkeypatch, ticker="600919.SH", *, exchange="SSE", start=START, end=END):
    source, run = source_layout(tmp_path)
    canonical_query = ticker.replace(".SS", ".SH")
    reference = save_exact_snapshot(source, exact_snapshot(canonical_query))
    monkeypatch.setattr(bank, "current_time", lambda now=None: NOW + timedelta(seconds=10) if now is None else now)
    module = entry()
    session = module.Session(run, source=source, bank_snapshot=reference)
    session.clock = module.SharedSessionClock(clock=lambda: 100.0, wall_clock=lambda: (NOW + timedelta(seconds=10)).timestamp())
    session.selection = module.build_explicit_selection(ticker, END.isoformat(), DEFAULT_CONFIG)
    module.validate_selection(session.selection)
    authority(source)
    config = session.configure(DEFAULT_CONFIG)
    monkeypatch.setattr(config_module, "_config", config)
    monkeypatch.setattr(statement_acceptance, "SOURCE_ROOT", source)
    if start is not None:
        from tests.test_tushare_sse_prepared_calendar import rows
        values=rows(start,end,exchange)
        for row in values:
            row["pretrade_date"]=(date.fromisoformat(f"{row['cal_date'][:4]}-{row['cal_date'][4:6]}-{row['cal_date'][6:]}")-timedelta(days=1)).strftime("%Y%m%d")
        item, calls = publication(values, start=start, end=end,
                                  symbol="601328.SH" if exchange=="SSE" else "000001.SZ")
        assert len(calls) == 1  # synthetic provider calls only
        with MarketHistoryStore.open(MarketHistoryConfig.from_mapping(config)) as store:
            store.publish_session_calendar(item)
    return module, session, source, run

@pytest.mark.parametrize("ticker", ["600919.SH", "600919.SS", "601328.SH", "600895.SH", "600000.SH"])
def test_new_sse_bank_reaches_real_entry_calendar_precheck(tmp_path, monkeypatch, ticker):
    _, session, _, run = prepare_case(tmp_path, monkeypatch, ticker)
    config = session.precheck(DEFAULT_CONFIG)
    result = json.loads((run/"precheck.json").read_text())
    assert result["issues"] == []
    assert any(c["location"]=="calendar" and c["status"]=="verified_full_natural_date_window" for c in result["checked"])
    assert (run/"shared-binding.json").is_file()
    assert config["analysis_max_calls"] == 60
    assert config["llm_max_output_tokens"] == 65536
    assert config["llm_max_retries"] == 0
    assert config["analysis_timeout_seconds"] == 1200
    assert json.loads((run/"session.json").read_text())["resume"] is False
    assert not (run/"refresh").exists()
    assert not (run/"analysis").exists()


@pytest.mark.parametrize("case", ["retained_oct16", "no_calendar", "late_start", "szse_cannot_use_sse", "wrong_calendar_exchange", "missing_day", "future_observation"])
def test_entry_refuses_missing_wrong_or_untrustworthy_calendar(tmp_path, monkeypatch, case):
    ticker = "000001.SZ" if case=="szse_cannot_use_sse" else "600919.SH"
    start = None if case=="no_calendar" else (START+timedelta(days=1) if case=="late_start" else START)
    end = date(2026,10,16) if case=="retained_oct16" else END
    module, session, _, run = prepare_case(tmp_path, monkeypatch, ticker, exchange="SZSE" if case=="wrong_calendar_exchange" else "SSE", start=start, end=end)
    if case in {"missing_day", "future_observation"}:
        with MarketHistoryStore.open(MarketHistoryConfig.from_mapping(session.configure(DEFAULT_CONFIG))) as store:
            if case=="missing_day":
                store._connection.execute("DELETE FROM market_sessions WHERE session_date='2026-10-01'")
            else:
                store._connection.execute("UPDATE market_session_calendars SET observed_at='2099-01-01T00:00:00+00:00'")
            store._connection.commit()
    with pytest.raises(module.Stop, match="market_calendar_not_ready"):
        session.precheck(DEFAULT_CONFIG)
    assert json.loads((run/"precheck.json").read_text())["issues"]
    assert not (run/"shared-binding.json").exists()
    assert not (run/"refresh").exists()

def test_szse_identity_reaches_entry_with_its_generic_calendar(tmp_path, monkeypatch):
    from tests.test_tushare_sse_prepared_calendar import persist_baostock

    _, session, _, run = prepare_case(tmp_path, monkeypatch, "000001.SZ", start=None)
    configured = session.configure(DEFAULT_CONFIG)
    cursor = START
    while cursor <= END:
        last = min(cursor + timedelta(days=729), END)
        persist_baostock((configured, MarketHistoryConfig.from_mapping(configured)), start=cursor, end=last)
        cursor = last + timedelta(days=1)
    session.precheck(DEFAULT_CONFIG)
    result = json.loads((run / "precheck.json").read_text())
    assert not result["issues"]
    assert any(item["location"] == "calendar" for item in result["checked"])
    assert (run / "shared-binding.json").is_file()
    assert not (run / "refresh").exists()


@pytest.mark.parametrize("case", ["expired_identity", "before_listing", "wrong_exchange_alias"])
def test_identity_limits_still_precede_calendar_admission(tmp_path, monkeypatch, case):
    _, session, _, run = prepare_case(tmp_path, monkeypatch)
    if case=="expired_identity":
        monkeypatch.setattr(bank,"current_time",lambda now=None: NOW+timedelta(days=1,seconds=1))
    elif case=="before_listing":
        session.selection["analysis_date"]="1999-12-31"
    else:
        session.selection["ticker"]="600919.SZ"
    with pytest.raises(bank.BankIdentityError):
        session.precheck(DEFAULT_CONFIG)
    assert not (run/"precheck.json").exists()
    assert not (run/"shared-binding.json").exists()

def test_v2_calendar_is_exchange_bound_but_legacy_proof_remains_symbol_bound(tmp_path, monkeypatch):
    from tradingagents.market_history import current_cache as cache
    _, session, _, _ = prepare_case(tmp_path, monkeypatch)
    config=session.precheck(DEFAULT_CONFIG)
    monkeypatch.setattr(config_module,"_config",config)
    with MarketHistoryStore.open(MarketHistoryConfig.from_mapping(config)) as store:
        calendar=cache.read_prepared_exchange_calendar(store,"600919.SS",START,END,config=config)
        assert cache.read_prepared_sse_calendar(store,"600919.SS",START,END) is None
        legacy=cache.read_prepared_sse_calendar(store,"601328.SS",START,END)
    proof=cache.calendar_evidence(calendar)
    assert proof["proof"]["kind"]==cache.SSE_EXCHANGE_CALENDAR_PROOF
    assert proof["proof"]["exchange_scope"]=="SSE"
    assert "symbol_scope" not in proof["proof"]
    assert "pretrade_date" in proof["proof"]["raw_payload"]
    assert cache.calendar_evidence(legacy)["proof"]["symbol_scope"]=="601328.SS"
    assert calendar.raw_payload==legacy.raw_payload and calendar.calendar_revision_id==legacy.calendar_revision_id
    assert not calendar.provider.strict_history_qualified

@pytest.mark.parametrize("field,value", [("pretrade_date","not-a-date"),("pretrade_date","20990231"),("unregistered_field","synthetic-extension")])
def test_v2_rejects_wrong_optional_provider_field_or_unknown_extension(tmp_path,monkeypatch,field,value):
    from dataclasses import replace

    from tradingagents.market_history import current_cache as cache
    _,session,_,_=prepare_case(tmp_path,monkeypatch)
    config=session.precheck(DEFAULT_CONFIG)
    with MarketHistoryStore.open(MarketHistoryConfig.from_mapping(config)) as store:
        calendar=cache.read_prepared_exchange_calendar(store,"600919.SH",START,END,config=config)
    raw=json.loads(calendar.raw_payload)
    raw["calendar"][0][field]=value
    changed=replace(calendar,raw_payload=json.dumps(raw,sort_keys=True).encode())
    # Digest changes too; even a self-consistent rehashed extension cannot become an admitted proof.
    with pytest.raises(ValueError):
        cache.validate_calendar_evidence(cache.calendar_evidence(changed))

def test_unrecognized_suffix_does_not_supply_authoritative_exchange(tmp_path,monkeypatch):
    from tradingagents.market_history import current_cache as cache
    _,session,_,_=prepare_case(tmp_path,monkeypatch)
    config=session.precheck(DEFAULT_CONFIG)
    with pytest.raises(ValueError):
        cache._trusted_calendar_exchange("999999.SS",config)

def test_calendar_sharing_keeps_price_cache_security_keys_distinct(tmp_path,monkeypatch):
    from tradingagents.dataflows import market_snapshot
    from tradingagents.market_history import current_cache as cache
    _,session,_,_=prepare_case(tmp_path,monkeypatch)
    config=session.precheck(DEFAULT_CONFIG)
    monkeypatch.setattr(config_module,"_config",config)
    descriptor=market_snapshot.SNAPSHOT_PROVIDERS["akshare"]
    _,first=cache.binding("600919.SS","akshare",descriptor)
    _,second=cache.binding("601328.SS","akshare",descriptor)
    assert first.instrument_id!=second.instrument_id and first.canonical_symbol!=second.canonical_symbol

@pytest.mark.parametrize("ticker", ["600919.SH","601328.SH"])
@pytest.mark.parametrize("change", ["ingestion", "latest_partial", "latest_complete"])
@pytest.mark.usefixtures("validated_manifest")
def test_new_identity_public_snapshot_cache_hit_and_lifecycle_recheck(tmp_path,monkeypatch,ticker,change):
    """Real public cache path, synthetic price loader only; not a budgeted model run."""
    import copy
    from dataclasses import replace

    import pandas as pd

    from tradingagents.dataflows import market_snapshot
    from tradingagents.dataflows.errors import NoMarketDataError
    from tradingagents.market_history import calendar_discovery

    _,session,_,_=prepare_case(tmp_path,monkeypatch,ticker)
    bound=session.precheck(DEFAULT_CONFIG)
    # Isolated codec acceptance uses the ordinary non-execution config; entry budget
    # limits were asserted above. This never grants a physical network capability.
    config=copy.deepcopy(DEFAULT_CONFIG)
    config.update({key:value for key,value in bound.items() if key in session.paths})
    config.update(instrument_identity_registry_sha256=bound["instrument_identity_registry_sha256"],
        market_history_mode="current",data_usage_mode="personal_research",
        market_data_vendors={"cn_a":{"core_stock_apis":"akshare"}},mainland_capability_routing_mode="legacy")
    monkeypatch.setattr(config_module,"_config",config)
    start,end=date(2026,10,1),END
    canonical=ticker.replace(".SH",".SS")
    frame=pd.DataFrame({"Date":pd.bdate_range(start,end),"Open":10.0,"High":12.0,
                       "Low":9.0,"Close":11.0,"Volume":100.0})
    calls=[]
    def load(symbol,begin,finish,*,physical_request=None,**kwargs):
        assert symbol==canonical and (begin,finish)==(str(start),str(end))
        calls.append(symbol)
        return physical_request("synthetic-price",lambda:frame.copy())
    monkeypatch.setattr(calendar_discovery,"ensure_mainland_calendar_window",
                        lambda *a,**kw:pytest.fail("retained calendar hit must not acquire"))
    monkeypatch.setitem(market_snapshot.SNAPSHOT_PROVIDERS,"akshare",
        replace(market_snapshot.SNAPSHOT_PROVIDERS["akshare"],load=load,
                uses_typed_physical_requests=True,history_load=None))
    live=market_snapshot.get_authoritative_market_snapshot(canonical,str(start),str(end),minimum_history_rows=21)
    cached=market_snapshot.get_authoritative_market_snapshot(canonical,str(start),str(end),minimum_history_rows=21)
    assert cached.history_store_status=="current_only_cached",cached.history_store_diagnostic
    assert calls==[canonical] and len(live.physical_attempt_events)==1 and not cached.physical_attempt_events
    assert live.snapshot_id==cached.snapshot_id and live.retrieved_at==cached.retrieved_at
    assert cached.frame["Date"].max().date() == date(2026,10,30)  # synthetic weekend baseline: no invented Sunday row
    from tradingagents.market_history import current_cache as cache
    other = "601328.SS" if canonical == "600919.SS" else "600919.SS"
    foreign, diagnostic = cache.try_read(other, str(start), str(end), ("akshare",),
                                        market_snapshot.SNAPSHOT_PROVIDERS, 21)
    assert foreign is None and diagnostic.startswith("current_cache_miss:")
    assert calls == [canonical], "another security must not hit this retained price frame"
    with MarketHistoryStore.open(MarketHistoryConfig.from_mapping(config)) as store:
        if change == "ingestion":
            store._connection.execute("UPDATE ingestion_runs SET requested_start='2021-11-02' WHERE operation='calendar_refresh'")
            store._connection.commit()
        else:
            from tests.test_tushare_sse_prepared_calendar import OBSERVED
            newer, _ = publication(start=start + timedelta(days=1) if change == "latest_partial" else START, end=END)
            store.publish_session_calendar(replace(newer, observed_at=OBSERVED + timedelta(seconds=1)))
    if change == "latest_complete":
        refreshed = market_snapshot.get_authoritative_market_snapshot(canonical,str(start),str(end),minimum_history_rows=21)
        assert len(refreshed.frame) == len(frame)
        assert calls == [canonical, canonical], "valid new full calendar invalidates old proof and permits independent reacquisition"
        return
    with pytest.raises((ValueError,NoMarketDataError)):
        market_snapshot.get_authoritative_market_snapshot(canonical,str(start),str(end),minimum_history_rows=21)
    assert calls==[canonical], "corrupt retained calendar cannot be bypassed by a new price request"

"""Offline candidate. Store, bound identity config and release verifier are owned by the caller."""
from datetime import date,timedelta
from zoneinfo import ZoneInfo
import sys, os, sqlite3, json, runpy
from datetime import datetime
from pathlib import Path
ROOT = Path(__file__).resolve().parents[1]
if __name__ == "__main__":
    def offline(event, args):
        if event in {"socket.connect", "socket.getaddrinfo", "socket.sendto", "subprocess.Popen"}:
            raise PermissionError("offline_calendar_reader")
    sys.addaudithook(offline)
    sys.path.insert(0, str(ROOT))
from tradingagents.market_history.current_cache import _trusted_calendar_exchange,read_prepared_exchange_calendar
from tradingagents.market_history.calendar_discovery import read_mainland_calendar_window

SH=ZoneInfo("Asia/Shanghai")

def select(store,config,symbol,request,now,*,release_root=None,manifest=None,verifier=None,daily_run=None,run_calendar=None):
    if now.utcoffset() is None:
        raise ValueError("timezone_required")
    _,identity=_trusted_calendar_exchange(symbol,config)
    today=now.astimezone(SH).date()
    latest=request=="latest"
    day=today if latest else date.fromisoformat(request)
    if day>today:raise ValueError("future_date")
    policy=None
    while True:
        generic=run_calendar if run_calendar is not None else read_mainland_calendar_window(store,day,day)
        calendar=generic
        scoped_calendar=run_calendar
        if run_calendar is None and identity.venue in {"XSHG","XSHE"}:
            try:
                exchange=read_prepared_exchange_calendar(store,symbol,day,day,config=config)
            except ValueError as error:
                if str(error) not in {"prepared SSE calendar range incomplete","prepared SZSE calendar range incomplete"} or generic is None:raise
                exchange=None
            if exchange is not None:
                scoped=[s for s in exchange.sessions if s.session_date==day]
                if len(scoped)!=1:raise ValueError("calendar_coverage_missing")
                if generic is not None:
                    common=[s for s in generic.sessions if s.session_date==day]
                    if len(common)!=1 or common[0].status!=scoped[0].status:
                        raise ValueError("calendar_membership_conflict")
                scoped_calendar=exchange
                if generic is None:calendar=exchange
        if latest and daily_run:
            if scoped_calendar is None:raise ValueError("calendar_coverage_missing")
            calendar=scoped_calendar
        if calendar is None:raise ValueError("calendar_coverage_missing")
        if calendar.observed_at.utcoffset() is None or calendar.observed_at>now:
            raise ValueError("calendar_not_observed")
        if latest and scoped_calendar is not None:
            from full_auction_schedule import check_calendar_freshness
            check_calendar_freshness(scoped_calendar.observed_at,now)
        rows=[s for s in calendar.sessions if s.session_date==day]
        if len(rows)!=1:raise ValueError("calendar_coverage_missing")
        session=rows[0]
        if session.status.value=="open":
            close=session.closes_at
            if latest and scoped_calendar is not None:
                from full_auction_schedule import schedule
                policy=schedule(identity.symbol,identity.venue,day,now)
                scheduled=datetime.fromisoformat(policy["scheduled_auction_end"])
                if close is not None and close!=scheduled:raise ValueError("special_session")
                close=scheduled
            if day<today or (close is not None and close<=now):break
            if close is None:raise ValueError("verified_close_time_missing")
        if not latest:raise ValueError("no_completed_session")
        day-=timedelta(days=1)
    matched = None
    if latest or day==today:
        if close is None:raise ValueError("verified_close_time_missing")
        if verifier is None:raise ValueError("security_close_missing")
        key=("config/full_evidence/market-releases.json" if identity.symbol=="601328.SS"
             else f"config/full_evidence/{identity.symbol}/market-releases.json")
        need=None
        if policy is not None:
            from full_auction_schedule import reject_known_exception
            reject_known_exception(release_root,identity.symbol,day,close)
            need=dict(ticker=identity.symbol.replace(".SS",".SH"),venue=identity.venue,trading_date=str(day),
                      calendar_revision=calendar.calendar_revision_id,auction_policy=policy)
        elif (day==today and latest and session.closes_at is not None and session.closes_at<=now
                and now.astimezone(SH).hour>=16 and identity.venue=="XSHG"):
            need=dict(ticker=identity.symbol.replace(".SS",".SH"),venue=identity.venue,trading_date=str(day),
                      calendar_revision=calendar.calendar_revision_id,close_at=session.closes_at.isoformat())
        if need and daily_run and (daily_run/"intake-daily/registration.json").is_file():
            from full_cold_daily import read_registered
            matched=read_registered(release_root,daily_run,need)
            if not close<=verifier["instant"](matched["first_observed_at"])<=now:raise ValueError("daily_observation_time")
        else:
            reviewed={x["relative"] for x in manifest["files"] if x["root"]=="project"}
            if key not in reviewed and need and daily_run:
                if policy and now < datetime.fromisoformat(policy["earliest_daily_read"]):
                    raise ValueError("daily_window_not_open")
                raise DailyRequired(need)
            packet=verifier["bound"](release_root,manifest,key)
            if packet.get("ticker")!=identity.symbol:raise ValueError("wrong_security")
            if close is None:raise ValueError("verified_close_time_missing")
            for row in packet["rows"]:
                if row.get("trading_date")!=str(day):continue
                if policy is not None and packet.get("source")=="https://api.waditu.com/dataapi/daily":
                    qualified=verifier["tushare_daily_close"](release_root,manifest,packet,row,current_policy=policy)
                else:
                    qualified=verifier["observed_close"](release_root,manifest,packet,row)
                if qualified is None:continue
                if policy is not None and packet.get("source")!="https://api.waditu.com/dataapi/daily":
                    original=verifier["original_json"](release_root,manifest,row)
                    if qualified.get("scope")!="auction_close_price" or original["close"]<=0:continue
                seen=verifier["instant"](qualified["first_observed_at"])
                pub=qualified.get("published_at")
                if close<=seen<=now and (pub is None or close<=verifier["instant"](pub)<=seen):
                    matched = qualified
                    break
            else:raise ValueError("security_close_missing")
    return dict(trading_date=str(day),venue=identity.venue,security=identity.symbol,
                calendar_revision=calendar.calendar_revision_id, calendar_observed_at=calendar.observed_at.isoformat(),
                auction_schedule=policy,close_evidence=matched, purpose="current_retrospective_selection",counts_as_full_acceptance=False)

class DailyRequired(ValueError):
    pass

def main():
    from tradingagents.market_history.store import MarketHistoryStore
    from tradingagents.market_history.config import MarketHistoryConfig, MarketHistoryMode, DataUsageMode
    from tradingagents.default_config import DEFAULT_CONFIG
    entry = runpy.run_path(str(ROOT / "scripts/analyze_mainland_bounded.py"))
    verifier = runpy.run_path(str(ROOT / "scripts/full_date_evidence.py"))
    manifest = entry["read_json"](ROOT / "config/bounded_mainland_release.json")
    registry = "config/instrument_identity_registry.json"
    pin = next(row["sha256"] for row in manifest["files"] if row["root"] == "project"
               and row["relative"].replace(chr(92), "/") == registry)
    cfg = dict(DEFAULT_CONFIG, instrument_identity_registry_path=str(ROOT/registry),
               instrument_identity_registry_sha256=pin)
    base = ROOT / "data/market_history"
    run_calendar=None
    if len(sys.argv)==7:
        import re
        from contextlib import closing
        sys.path.insert(0,str(ROOT/"scripts"))
        ids=set(sys.argv[4:])-{"-"}
        if len(ids)!=1 or not re.fullmatch("[0-9a-f]{32}",next(iter(ids))):raise ValueError("invalid_run")
        run=ROOT/"reports/bounded-runs"/next(iter(ids))
        if sys.argv[4]!="-":
            from full_cold_identity import reader_config
            cfg.update(reader_config(ROOT,run,sys.argv[1]))
        if sys.argv[5]!="-":
            from full_financial_channel import binding,load,digest,ordinary
            spec=binding(ROOT,run);record=load(run/"intake-calendar-revision.json")
            if (record["run_id"]!=run.name or record["venue"]!=spec["params"]["venue"] or record["venue"]!=("XSHE" if sys.argv[1].endswith(".SZ") else "XSHG")
                or spec["params"]["ticker"]!=sys.argv[1].replace(".SS",".SH")):raise ValueError("calendar_owner_mismatch")
            with closing(sqlite3.connect(ordinary(run/"physical-ledger/ledger.sqlite3").as_uri()+"?mode=ro",uri=True)) as ledger:
                admitted=ledger.execute("select digest,origin,cost from evidence where source='intake_calendar'").fetchall()
                claimed=ledger.execute("select params,run,terminal from requests where id=?",(run.name,)).fetchone()
            if admitted!=[(digest(record),("verified-szse-calendar" if record["venue"]=="XSHE" else "verified-sse-calendar"),1)] or not claimed or not claimed[1] or claimed[2] is not None or claimed[0]!=spec["params_sha256"]:raise ValueError("calendar_owner_mismatch")
            from hashlib import sha256
            if record["wire_sha256"]!=sha256(ordinary(run/"intake-calendar-response.bin").read_bytes()).hexdigest():raise ValueError("calendar_owner_mismatch")
            base=run/"intake-calendar"
    elif len(sys.argv)!=4:raise ValueError("invalid_run")
    db=base/"market_history.sqlite3"
    marker = base / "payloads/.market-history-database-owner"
    for path in (db, marker):
        entry["regular_path"](path)
    if marker.read_bytes() != os.path.normcase(str(db)).encode() + b"\n":
        raise ValueError("calendar_store_owner_mismatch")
    con = sqlite3.connect(db.as_uri()+"?mode=ro", uri=True)
    try:
        con.execute("PRAGMA query_only=ON")
        con.execute("BEGIN")
        store = MarketHistoryStore(MarketHistoryConfig(MarketHistoryMode.CURRENT, db, base/"payloads",
                                   base/"backups", DataUsageMode.PERSONAL_RESEARCH), con)
        if len(sys.argv)==7 and sys.argv[5]!="-":
            revisions=con.execute("select calendar_revision_id from market_session_calendars").fetchall()
            if revisions!=[(record["revision"],)]:raise ValueError("calendar_owner_mismatch")
            from full_exchange_revision import verify
            run_calendar=verify(store,record["revision"],sys.argv[1],ordinary(run/"intake-calendar-response.bin").read_bytes())
        return select(store, cfg, sys.argv[1], sys.argv[2], datetime.fromisoformat(sys.argv[3]),
                      release_root=ROOT, manifest=manifest, verifier=verifier,daily_run=run if len(sys.argv)==7 else None,run_calendar=run_calendar)
    finally:
        con.close()

if __name__ == "__main__":
    try:
        print(json.dumps(main()))
    except DailyRequired as error:
        print(json.dumps(dict(error="same_day_close_required",need=error.args[0])))
        sys.exit(2)
    except Exception as error:
        known = {"latest_calendar_expired","daily_window_not_open","auction_rule_inapplicable","special_session","known_security_suspension","security_close_missing", "wrong_security", "calendar_coverage_missing", "calendar_not_observed",
                 "prepared SSE calendar range incomplete","prepared SZSE calendar range incomplete", "no_completed_session", "verified_close_time_missing",
                 "reviewed_input_missing", "calendar_membership_conflict", "calendar_store_owner_mismatch", "future_date"}
        print(json.dumps({"error": str(error) if str(error) in known else "calendar_evidence_unverified"}))
        sys.exit(2)




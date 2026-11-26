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

_DAILY_SOURCE = "https://api.waditu.com/dataapi/daily"


def _qualified_close(root, manifest, verifier, packet, row, policy, close, now):
    daily = packet.get("source") == _DAILY_SOURCE
    if daily and policy is not None:
        qualified = verifier["tushare_daily_close"](root, manifest, packet, row, current_policy=policy)
    else:
        qualified = verifier["observed_close"](root, manifest, packet, row)
    if qualified is None:
        return None
    if policy is not None and not daily:
        original = verifier["original_json"](root, manifest, row)
        if qualified.get("scope") != "auction_close_price" or original["close"] <= 0:
            return None
    seen = verifier["instant"](qualified["first_observed_at"])
    if seen > now or (close is not None and seen < close):
        return None
    published = qualified.get("published_at")
    if published is not None:
        published = verifier["instant"](published)
        if published > seen or (close is not None and published < close):
            return None
    return qualified


def _verified_older_close_rows(release_root, manifest, verifier, packet, day, now, policy):
    """Only verified older evidence may fall through to same-session capture."""
    rows = packet.get("rows")
    if not isinstance(rows, list) or not rows:
        return False
    for row in rows:
        if not isinstance(row, dict) or not isinstance(row.get("trading_date"), str):
            return False
        raw_date = row["trading_date"]
        try:
            row_date = date.fromisoformat(raw_date)
        except ValueError:
            return False
        if raw_date != row_date.isoformat() or row_date >= day:
            return False

    for row in rows:
        row_date = date.fromisoformat(row["trading_date"])
        from full_auction_schedule import EFFECTIVE, reject_known_exception, schedule
        old_policy = None
        close = None
        if row_date >= EFFECTIVE:
            old_policy = schedule(packet["ticker"], policy["venue"], row_date, now)
            close = datetime.fromisoformat(old_policy["scheduled_auction_end"])
            reject_known_exception(release_root, packet["ticker"], row_date, close)
        elif packet.get("source") != _DAILY_SOURCE:
            # No reviewed auction cutoff for this date. The legacy daily verifier
            # separately enforces its own positive close and same-day observation.
            return False
        if _qualified_close(release_root, manifest, verifier, packet, row, old_policy, close, now) is None:
            return False
    return True


def _run_directory_identity(root):
    import stat
    from full_financial_channel import ordinary

    ordinary(root)
    info = root.lstat()
    if not stat.S_ISDIR(info.st_mode) or not info.st_ino:
        raise ValueError("daily_run_directory_invalid")
    return info.st_dev, info.st_ino


def _daily_attempt_unstarted(source, root, need):
    from contextlib import closing
    from full_financial_channel import binding, ordinary

    directory = _run_directory_identity(root)
    spec = binding(source, root)
    intent = spec["params"]
    if (intent.get("contract") != "default-full-latest-intake-v1"
            or intent.get("mode") != "default_full" or intent.get("requested_date") != "latest"
            or intent.get("ticker") != need["ticker"] or intent.get("venue") != need["venue"]):
        raise ValueError("daily_owner_mismatch")
    path = ordinary(root / "physical-ledger/ledger.sqlite3")
    with closing(sqlite3.connect(path.as_uri() + "?mode=ro", uri=True)) as ledger:
        claim = ledger.execute("select params,run,terminal from requests where id=?", (root.name,)).fetchone()
    if not claim or claim[0] != spec["params_sha256"] or not claim[1] or claim[2] is not None:
        raise ValueError("daily_owner_mismatch")
    try:
        (root / "intake-daily").lstat()
    except FileNotFoundError:
        # A vanished or exchanged parent can also yield FileNotFoundError.
        # Capture still exclusively creates the attempt directory before sending.
        if _run_directory_identity(root) != directory:
            raise ValueError("daily_run_directory_changed")
        return True
    return False


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
                if not _daily_attempt_unstarted(release_root, daily_run, need):
                    raise ValueError("security_close_missing")
                raise DailyRequired(need)
            packet=verifier["bound"](release_root,manifest,key)
            if packet.get("ticker")!=identity.symbol:raise ValueError("wrong_security")
            if close is None:raise ValueError("verified_close_time_missing")
            for row in packet["rows"]:
                if row.get("trading_date")!=str(day):continue
                matched = _qualified_close(release_root, manifest, verifier, packet, row, policy, close, now)
                if matched is not None:
                    break
            else:
                if (
                    policy is not None and day == today and need and daily_run
                    and _daily_attempt_unstarted(release_root, daily_run, need)
                    and _verified_older_close_rows(release_root, manifest, verifier, packet, day, now, policy)
                ):
                    if now < datetime.fromisoformat(policy["earliest_daily_read"]):
                        raise ValueError("daily_window_not_open")
                    raise DailyRequired(need)
                raise ValueError("security_close_missing")
    return dict(trading_date=str(day),venue=identity.venue,security=identity.symbol,
                calendar_revision=calendar.calendar_revision_id, calendar_observed_at=calendar.observed_at.isoformat(),
                auction_schedule=policy,close_evidence=matched, purpose="current_retrospective_selection",counts_as_full_acceptance=False)

class DailyRequired(ValueError):
    pass

def _read_calendar_evidence(run, record, mode):
    from full_financial_channel import ordinary
    from hashlib import sha256
    if "core_v1_sha256" in record:
        if (set(record) != {"run_id", "venue", "revision", "core_v1_sha256"}
                or record["venue"] != "XSHG" or mode != "default_full"):
            raise ValueError("calendar_owner_mismatch")
        evidence_file, expected = run/"intake-calendar-core.json", record["core_v1_sha256"]
    else:
        evidence_file, expected = run/"intake-calendar-response.bin", record["wire_sha256"]
    calendar_evidence = ordinary(evidence_file).read_bytes()
    if expected != sha256(calendar_evidence).hexdigest():
        raise ValueError("calendar_owner_mismatch")
    if "core_v1_sha256" in record:
        from datetime import datetime
        from daily_retention import unique
        from full_financial_channel import load
        from full_cold_calendar import _coverage, _predecessors
        query = load(run/"intake-calendar-attempt.json")["query"]
        fields = ["exchange", "cal_date", "is_open", "pretrade_date"]
        params = query["params"]
        start = datetime.strptime(params["start_date"], "%Y%m%d").date()
        from datetime import timedelta
        if (query["api_name"] != "trade_cal" or query["fields"] != ",".join(fields)
                or params["exchange"] != "SSE" or params["limit"] != 9 or params["offset"] != 0
                or params["end_date"] != (start + timedelta(days=7)).strftime("%Y%m%d")):
            raise ValueError("calendar_owner_mismatch")
        core = json.loads(calendar_evidence, object_pairs_hook=unique)
        data = core.get("data") if type(core) is dict else None
        if (type(data) is not dict or set(core) != {"code", "data"}
                or type(core["code"]) is not int or core["code"] != 0
                or set(data) - {"fields", "items", "has_more"} or data.get("has_more", False) is not False
                or _coverage(data, fields, "SSE", start) != "complete" or not _predecessors(data["items"])):
            raise ValueError("calendar_owner_mismatch")
    return calendar_evidence

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
            calendar_evidence = _read_calendar_evidence(run, record, spec["params"]["mode"])
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
            run_calendar=verify(store,record["revision"],sys.argv[1],calendar_evidence)
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




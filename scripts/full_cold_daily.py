"""Capture a single bound close on its trading day after verified close and 16:00 Shanghai."""
from datetime import datetime,timedelta,timezone
from hashlib import sha256
from zoneinfo import ZoneInfo
import json

def acquire(source,root,clock,need):
    from full_financial_channel import binding,write,digest
    from full_cold_identity import token_for_identity
    from tradingagents.full_session_ledger import owner,preparation_role
    from tradingagents.dataflows.bank_identity_acquisition import existing_coordinator,CaptureClock
    from tradingagents.market_history.coordinator import RequestPriority,upstream_service_identity_for_provider
    from tradingagents.tushare_https import send_once,HTTPS_BASE,TushareHTTPSFailure
    from daily_retention import decode
    own=owner();spec=binding(source,root);intent=spec["params"]
    if (intent["venue"] not in {"XSHG","XSHE"} or own is None or own.key!=root.name or own.params!=intent or own.child_role is not None or own.phase!="preparation"
        or own.path!=root/"physical-ledger"/"ledger.sqlite3" or intent["contract"]!="default-full-latest-intake-v1"):
        raise ValueError("daily_owner_required")
    sh=ZoneInfo("Asia/Shanghai")
    def valid_time():
        now=datetime.now(sh)
        if "auction_policy" in need:
            from datetime import date
            from full_auction_schedule import schedule
            day=date.fromisoformat(need["trading_date"])
            policy=schedule(intent["ticker"],intent["venue"],day,now)
            if (policy!=need["auction_policy"] or now<datetime.fromisoformat(policy["earliest_daily_read"])
                or not 0<=(now.date()-day).days<=7 or not need["calendar_revision"]
                or need["ticker"]!=intent["ticker"] or need["venue"]!=intent["venue"]):
                raise ValueError("daily_time_or_identity_unverified")
            from full_auction_schedule import reject_known_exception
            reject_known_exception(source,intent["ticker"].replace(".SH",".SS"),day,datetime.fromisoformat(policy["scheduled_auction_end"]))
            return now
        close=datetime.fromisoformat(need["close_at"])
        if (need["ticker"]!=intent["ticker"] or need["venue"]!=intent["venue"] or close.utcoffset() is None
            or need["trading_date"]!=now.date().isoformat() or close.astimezone(sh).date()!=now.date()
            or now<=max(close,now.replace(hour=16,minute=0,second=0,microsecond=0)) or not need["calendar_revision"]):
            raise ValueError("daily_time_or_identity_unverified")
        return now
    started=valid_time();day=datetime.fromisoformat(need["trading_date"]).date();sym=intent["ticker"];fields=["ts_code","trade_date","close"]
    params=dict(ts_code=sym,trade_date=day.strftime("%Y%m%d"),ts_type_name=HTTPS_BASE)
    url=HTTPS_BASE+"/daily";sub=root/"intake-daily";sub.mkdir(exist_ok=False)
    req=dict(method="POST",url=url,params=params,fields=fields,started_at=started.isoformat(),reserved=1)
    write(sub/"reservation.json",req)
    timer=CaptureClock();timer.end=min(timer.end,clock.deadline_monotonic);timer.end_epoch=min(timer.end_epoch,clock.effective_deadline())
    def send():
        valid_time();token=token_for_identity(source,intent["use_project_env"])
        try:
            valid_time()
            status,raw=send_once(url,dict(api_name="daily",params=params,fields=",".join(fields),token=token),min(8,timer.remaining()),coordinated_intake=True)
            if status!=200:raise ValueError("daily_status")
            close=decode(raw,day,token,symbol=sym)
        except TushareHTTPSFailure as error:
            if error.status_code!=429:raise
            from tradingagents.market_history.coordinator import PhysicalAttemptFailure,PhysicalAttemptOutcome
            delay=error.retry_after
            raise PhysicalAttemptFailure(outcome=PhysicalAttemptOutcome.RATE_LIMITED,retryable=False,status_code=429,
                error_code="daily_rate_limited",retry_after_seconds=delay if delay and 0<delay<=31536000 else 300) from None
        finally:del token
        observed=valid_time()
        return raw,dict(status="observed_daily_close",http_status=status,source=url,scope="auction_close_price",ticker=sym,
            trading_date=day.isoformat(),observed_at=observed.isoformat(),first_observed_at=observed.isoformat(),published_at=None,
            response_sha256=sha256(raw).hexdigest(),close=close)
    uid,_=upstream_service_identity_for_provider("tushare")
    with existing_coordinator(source) as (coord,storage),preparation_role("calendar"):
        storage.check_existing();result=coord.execute_direct_physical_request(request_key=root.name+":intake:daily",owner_id=root.name,
            upstream_service_id=uid,priority=RequestPriority.INCREMENTAL_REFRESH,now=timer.now,sleep=timer.sleep,
            lease_duration=timedelta(seconds=35),operation="intake:daily",physical_request=send,cooldown_scope="daily",record_at_physical_io=True)
    if result.physical_attempt_count!=1:raise ValueError("daily_attempt_mismatch")
    raw,rec=result.value
    with (sub/'response.bin').open('xb') as f:
        import os
        f.write(raw);f.flush();os.fsync(f.fileno())
    write(sub/'receipt.json',rec)
    summary=dict(run_id=root.name,params_sha256=spec["params_sha256"],need=need,raw_sha256=sha256(raw).hexdigest(),
        receipt_sha256=digest(rec),request_sha256=digest(req))
    wire=json.dumps(summary,ensure_ascii=False,sort_keys=True,separators=(",",":"),allow_nan=False).encode()
    own.register("intake_daily",wire,digest(summary),"same-session-daily",1)
    write(sub/'registration.json',summary)


def read_registered(source,root,need):
    import sqlite3
    from contextlib import closing
    from full_financial_channel import binding,load,ordinary,digest
    from full_date_evidence import checked_daily_documents
    spec=binding(source,root);folder=root/"intake-daily"
    reg=load(ordinary(folder/"registration.json"));rec=load(ordinary(folder/"receipt.json"));req=load(ordinary(folder/"reservation.json"))
    raw=ordinary(folder/"response.bin").read_bytes()
    if (reg["need"]!=need or spec["params"]["ticker"]!=need["ticker"] or spec["params"]["venue"]!=need["venue"] or
        reg["run_id"]!=root.name or reg["params_sha256"]!=spec["params_sha256"] or reg["receipt_sha256"]!=digest(rec)
        or reg["request_sha256"]!=digest(req) or reg["raw_sha256"]!=sha256(raw).hexdigest()):
        raise ValueError("daily_receipt_mismatch")
    with closing(sqlite3.connect(ordinary(root/"physical-ledger/ledger.sqlite3").as_uri()+"?mode=ro",uri=True)) as db:
        ev=db.execute("select digest,origin,cost from evidence where source='intake_daily'").fetchall()
        claim=db.execute("select params,run,terminal from requests where id=?",(root.name,)).fetchone()
    if ev!=[(digest(reg),"same-session-daily",1)] or not claim or claim[0]!=spec["params_sha256"] or not claim[1] or claim[2] is not None:raise ValueError("daily_owner_mismatch")
    url="https://api.waditu.com/dataapi/daily"
    row=dict(trading_date=need["trading_date"],published_at=None,observed_at=rec["observed_at"],raw_sha256=reg["raw_sha256"])
    obj=checked_daily_documents(dict(source=url,ticker=need["ticker"].replace(".SH",".SS")),row,json.loads(raw),rec,req,
        dict(adapter="tushare-daily-close-v1",source=url,scope="auction_close_price"),current_policy=need.get("auction_policy"))
    if obj is None:raise ValueError("unverified_daily_documents")
    return obj



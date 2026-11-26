"""Read bound public evidence only. Deliberately has no app, model or transport imports."""
from datetime import date,datetime,timedelta
from pathlib import Path
from hashlib import sha256
from zoneinfo import ZoneInfo
import json
SH=ZoneInfo("Asia/Shanghai")
def instant(s):
    value=datetime.fromisoformat(s)
    if value.utcoffset() is None:raise ValueError("timezone_required")
    return value
def bound(root,manifest,key):
    rows=[r for r in manifest["files"] if r["root"]=="project" and r["relative"].replace(chr(92),"/")==key]
    if len(rows)!=1:raise ValueError("reviewed_input_missing")
    path=root/key
    if path.resolve()!=path:raise ValueError("nonordinary_input")
    raw=path.read_bytes()
    if sha256(raw).hexdigest()!=rows[0]["sha256"]:raise ValueError("reviewed_input_changed")
    return json.loads(raw)
def verify_reference(root,manifest,row):
    # The reviewed mapping pins the original public bytes under a fixed namespace.
    reference=row.get("raw_sha256","")
    if len(reference)!=64 or any(ch not in "0123456789abcdef" for ch in reference):return False
    key="config/full_evidence/originals/"+reference+".bin"
    approved=[r for r in manifest["files"] if r["root"]=="project" and r["relative"].replace(chr(92),"/")==key and r["sha256"]==reference]
    path=root/key
    return len(approved)==1 and path.resolve()==path and path.is_file() and sha256(path.read_bytes()).hexdigest()==reference

def original_json(root, manifest, row):
    """Decode only content-addressed bytes admitted by the release manifest."""
    if not verify_reference(root, manifest, row):
        return None
    raw = (root / "config/full_evidence/originals" / (row["raw_sha256"] + ".bin")).read_bytes()
    if sha256(raw).hexdigest() != row["raw_sha256"]:
        return None
    try:
        def unique(pairs):
            obj = dict(pairs)
            if len(obj) != len(pairs):
                raise ValueError("duplicate_json_key")
            return obj
        return json.loads(raw,object_pairs_hook=unique)
    except (UnicodeError, ValueError):
        return None

def calendar_matches(root, manifest, projection):
    raw = original_json(root, manifest, projection)
    return isinstance(raw, dict) and all(key in raw and raw[key] == projection.get(key)
        for key in ("venue", "revision", "start", "days"))

def release_matches(root, manifest, packet, row):
    raw = original_json(root, manifest, row)
    receipt = original_json(root, manifest, {"raw_sha256": row.get("receipt_sha256", "")})
    if not isinstance(raw, dict) or not isinstance(receipt, dict):
        return False
    return (
        raw.get("kind") == "closing_market_release"
        and raw.get("ticker") == packet["ticker"]
        and raw.get("venue") == ("XSHE" if packet["ticker"].endswith(".SZ") else "XSHG")
        and raw.get("source") == receipt.get("source") == packet["source"]
        and receipt.get("http_status") == 200
        and receipt.get("response_sha256") == row["raw_sha256"]
        and raw.get("trading_date") == row.get("trading_date")
        and raw.get("published_at") == row.get("published_at")
        and bool(receipt.get("observed_at"))
        and receipt["observed_at"] == row.get("observed_at")
    )

def tushare_daily_close(root, manifest, packet, row, *, current_policy=None):
    """Read actual daily wire bytes and two receiver records under independent release bindings."""
    from math import isfinite
    url = "https://api.waditu.com/dataapi/daily"
    def document(key):
        return original_json(root, manifest, {"raw_sha256": row.get(key, "")})
    raw = document("raw_sha256")
    receipt = document("receipt_sha256")
    request = document("reservation_sha256")
    contract = original_json(root, manifest, {"raw_sha256": packet.get("contract_sha256", "")})
    return checked_daily_documents(packet,row,raw,receipt,request,contract,current_policy=current_policy)


def checked_daily_documents(packet,row,raw,receipt,request,contract,*,current_policy=None):
    from math import isfinite
    url = "https://api.waditu.com/dataapi/daily"
    day = row.get("trading_date")
    fields = ["ts_code", "trade_date", "close"]
    symbol = str(packet.get("ticker","")).replace(".SS",".SH")
    if not all(isinstance(x, dict) for x in (raw, receipt, request, contract)):
        return None
    if (contract != dict(adapter="tushare-daily-close-v1", source=url, scope="auction_close_price")
        or packet.get("source") != url or not __import__("re").fullmatch(r"[0-9]{6}\.(?:SS|SZ)" if current_policy else r"[0-9]{6}\.SS",packet.get("ticker",""))
        or request.get("method") != "POST" or request.get("url") != url
        or request.get("fields") != fields or request.get("reserved") != 1
        or not isinstance(day,str) or request.get("params") != dict(ts_code=symbol,
            trade_date=day.replace("-",""),ts_type_name="https://api.waditu.com/dataapi")
        or receipt.get("status") != "observed_daily_close" or receipt.get("http_status") != 200
        or receipt.get("source") != url or receipt.get("response_sha256") != row["raw_sha256"]
        or receipt.get("scope") != "auction_close_price" or receipt.get("ticker") != symbol
        or receipt.get("trading_date") != day or receipt.get("published_at") is not None
        or row.get("published_at") is not None or receipt.get("observed_at") != row.get("observed_at")
        or receipt.get("first_observed_at") != receipt.get("observed_at")):
        return None
    started = instant(request["started_at"]).astimezone(SH)
    observed = instant(receipt["observed_at"]).astimezone(SH)
    if current_policy is not None:
        from full_auction_schedule import schedule
        venue="XSHE" if symbol.endswith(".SZ") else "XSHG"
        if (current_policy!=schedule(symbol,venue,date.fromisoformat(day),observed)
            or not datetime.fromisoformat(current_policy["earliest_daily_read"])<=started<=observed
            or (observed.date()-date.fromisoformat(day)).days>7):
            return None
    elif not datetime.fromisoformat(day+"T16:00:00").replace(tzinfo=SH)<started<=observed or observed.date().isoformat()!=day:
        return None
    import re
    if (set(raw)-{"code","msg","data","request_id"} or raw.get("msg") not in (None,"")
        or ("request_id" in raw and (not isinstance(raw["request_id"],str)
            or not re.fullmatch(r"[A-Za-z0-9_-]{1,80}",raw["request_id"])))):
        return None
    table=raw.get("data")
    if type(raw.get("code")) is not int or raw["code"]!=0 or not isinstance(table,dict) or set(table)-{"fields","items","has_more"}:
        return None
    cols=table.get("fields");items=table.get("items")
    if (not isinstance(cols,list) or sorted(cols)!=sorted(fields) or table.get("has_more")
        or not isinstance(items,list) or len(items)!=1 or not isinstance(items[0],list) or len(items[0])!=3):
        return None
    cell=dict(zip(cols,items[0]))
    if (cell["ts_code"]!=symbol or cell["trade_date"]!=day.replace("-","")
        or type(cell["close"]) not in (int,float) or not isfinite(cell["close"])
        or cell["close"]<=0 or cell["close"]!=receipt.get("close")):
        return None
    return dict(row,first_observed_at=receipt["observed_at"],scope="auction_close_price",
                basis="observed_final_record",published_at=None)

def observed_close(root, manifest, packet, row):
    """Qualify an exact version against an independently reviewed source contract."""
    if packet.get("source") == "https://api.waditu.com/dataapi/daily":
        return tushare_daily_close(root, manifest, packet, row)
    if not release_matches(root, manifest, packet, row):
        return None
    raw = original_json(root, manifest, row)
    policy = original_json(root, manifest, {"raw_sha256": packet.get("contract_sha256", "")})
    if not isinstance(policy, dict) or policy.get("venue") != ("XSHE" if packet["ticker"].endswith(".SZ") else "XSHG"):
        return None
    scope = raw.get("scope")
    if (policy.get("official_close_source") != raw["source"]
            or scope not in ("auction_close_price", "final_daily_ohlcv")
            or scope not in policy.get("scopes", [])
            or raw.get("final") is not True):
        return None
    from math import isfinite
    fields = ("close",) if scope == "auction_close_price" else ("open","high","low","close","volume")
    if any(type(raw.get(k)) not in (float,int) or not isfinite(raw[k]) or raw[k] < 0 for k in fields):
        return None
    if scope == "final_daily_ohlcv":
        end = policy.get("post_session_end", {}).get(raw["trading_date"])
        if not end or instant(end).astimezone(SH).date().isoformat() != raw["trading_date"]:
            return None
        if instant(row["observed_at"]) <= instant(end):
            return None
    return dict(row, first_observed_at=row["observed_at"], scope=scope,
                basis="observed_final_record", published_at=raw.get("published_at"))

def resolve(request,now,calendar,market):
    if now.utcoffset() is None:raise ValueError("timezone_required")
    today=now.astimezone(SH).date()
    if calendar["venue"]!=("XSHE" if market["ticker"].endswith(".SZ") else "XSHG") or not __import__("re").fullmatch(r"[0-9]{6}\.(SS|SZ)",market["ticker"]) or not calendar["revision"] or not market["source"]:
        raise ValueError("source_binding")
    rows=calendar["days"]
    expected=date.fromisoformat(calendar["start"])
    closes={}
    for row in rows:
        day=date.fromisoformat(row["day"])
        if day!=expected:raise ValueError("calendar_gap")
        expected+=timedelta(days=1)
        if row["close"] is not None:
            close=instant(row["close"])
            if close.astimezone(SH).date()!=day:raise ValueError("close_date_mismatch")
            closes[day]=close
    if request!="latest":
        day=date.fromisoformat(request)
        if day not in closes or closes[day]>now:raise ValueError("not_completed_session")
        return str(day)
    if not date.fromisoformat(calendar["start"])<=today<expected:raise ValueError("calendar_stale")
    eligible=[]
    for release in market["rows"]:
        day=date.fromisoformat(release["trading_date"])
        if release.get("published_at") is None:
            first = release.get("first_observed_at")
            if release.get("basis") == "observed_final_record" and first and day in closes and closes[day] <= instant(first) <= now:
                eligible.append(day)
            continue
        if release.get("observed_at") is None:continue
        published=instant(release["published_at"]);observed=instant(release["observed_at"])
        if day in closes and closes[day]<=published<=observed<=now:
            eligible.append(day)
    if not eligible:raise ValueError("no_verified_published_session")
    return str(max(eligible))
def eligible_release(row, calendar, now, ticker="601328.SS"):
    try:
        resolve("latest", now, calendar, dict(ticker=ticker, source="bound", rows=[row]))
        return True
    except ValueError:
        return False

def separate(row,target,decision_at,review_at):
    """Only classify; these records never feed graph state or acceptance."""
    if not row.get("source") or row.get("verified") is not True:return "unverified_source"
    if not row.get("observed_at"):return "unknown_observation"
    obs=instant(row["observed_at"])
    if obs>review_at:return "future_observation"
    if not row.get("trading_date"):return "unknown_trading_date"
    if row["trading_date"]!=target:return "different_trading_date"
    if not row.get("published_at"):return "unknown_publication"
    pub=instant(row["published_at"])
    if pub>obs:return "invalid_publication_time"
    if pub>decision_at or obs>decision_at:
        return "retrospective_only" if row.get("official_history") is True else "late_snapshot"
    return "available_by_decision_time"
def publish(root, manifest, report_dir, target, now, *, ticker="601328.SS"):
    from tempfile import NamedTemporaryFile
    import os
    key="config/full_evidence/latest-dynamics.json" if ticker=="601328.SS" else f"config/full_evidence/{ticker}/latest-dynamics.json"
    if not any(r["root"]=="project" and r["relative"].replace(chr(92),"/")==key for r in manifest["files"]):
        items=[];state="no_reviewed_feed"
    else:
        packet=bound(root,manifest,key)
        if packet["ticker"]!=ticker:raise ValueError("wrong_dynamic_instrument")
        items=[dict(r,verified=(r.get("verified") is True and verify_reference(root,manifest,r))) for r in packet["rows"]];state="reviewed_feed"
    cal=bound(root,manifest,"config/full_evidence/shenzhen-calendar.json" if ticker.endswith(".SZ") else "config/full_evidence/shanghai-calendar.json")
    decision=instant(next(r["close"] for r in cal["days"] if r["day"]==target))
    classified=[dict(source=r.get("source"),observed_at=r.get("observed_at"),trading_date=r.get("trading_date"),published_at=r.get("published_at"),classification=separate(r,target,decision,now),summary=r.get("summary") if r.get("source") and r.get("verified") is True and r.get("observed_at") and instant(r["observed_at"])<=now else None) for r in items]
    history=(report_dir/"full-result.json").read_bytes()
    output=dict(historical_report="full-result.json",historical_sha256=sha256(history).hexdigest(),target=target,decision_time=decision.isoformat(),reviewed_at=now.isoformat(),state=state,latest_dynamics=classified,used_by_model=False,counts_for_acceptance=False,eligible_for_backtest=False)
    with NamedTemporaryFile(dir=report_dir,delete=False) as tmp:
        tmp.write(json.dumps(output,ensure_ascii=False,indent=2).encode());name=tmp.name
    try:os.rename(name,report_dir/"latest-dynamics.json")
    finally:
        if os.path.exists(name):os.unlink(name)



def completed_historical_day(root,manifest,key,request,venue,now):
    """A completed calendar day supports explicit retrospective selection only."""
    record=bound(root,manifest,key)
    handoff=original_json(root,manifest,{"raw_sha256":record.get("handoff_sha256","")})
    payload=original_json(root,manifest,record)
    if not isinstance(handoff,dict) or not isinstance(payload,dict):
        raise ValueError("calendar_record_original_missing")
    ctx=handoff["context"]; result=handoff["result"]
    if (venue!="XSHG" or request>=now.astimezone(SH).date().isoformat()
       or record["venue"]!=venue or record["day"]!=request or ctx["venue"]!=venue
       or ctx["destination"]!="https://api.waditu.com/dataapi/trade_cal"
       or result["status"]!="ready" or result["revision"]!=record["revision"]
       or ctx["analysis_date"]!=request or result["analysis_date"]!=request
       or payload["normalizer_version"]!="tushare-market-history-normalizer-v1"
       or payload["adjustment_methodology"]!="tushare-adj-factor-divide-end-anchor-qfq-v1"
       or instant(record["observed_at"])>now):
        raise ValueError("calendar_record_binding")
    rows=[x for x in payload["calendar"] if x["cal_date"]==request.replace("-","")]
    if len(rows)!=1 or rows[0]["exchange"]!="SSE" or rows[0]["is_open"]!="1":
        raise ValueError("not_completed_session")
    sessions=[]
    for x in payload["calendar"]:
        day=datetime.strptime(x["cal_date"],"%Y%m%d").date().isoformat()
        if x["exchange"]!="SSE" or x["is_open"] not in ("0","1"):raise ValueError("invalid_calendar_row")
        sessions.append(dict(session_date=day,status="open" if x["is_open"]=="1" else "closed",opens_at=None,closes_at=None))
    sessions.sort(key=lambda x:x["session_date"])
    if any(date.fromisoformat(b["session_date"])-date.fromisoformat(a["session_date"])!=timedelta(days=1) for a,b in zip(sessions,sessions[1:])):raise ValueError("calendar_gap")
    fields=dict(payload_digest=record["raw_sha256"],provider_dataset_id=ctx["refresh_source"],reference_market="mainland-cn",timezone_name="Asia/Shanghai",sessions=sessions)
    rev=sha256(json.dumps(dict(fields=fields,kind="market-session-calendar"),sort_keys=True,separators=(",",":"),ensure_ascii=False).encode()).hexdigest()
    if result["revision"]!="market-session-calendar=sha256:"+rev:raise ValueError("calendar_revision_mismatch")

    return request


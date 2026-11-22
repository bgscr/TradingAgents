"""Candidate callback for an already authorized bounded owner. No credential discovery."""
from datetime import date, datetime, time, timezone
from hashlib import sha256
import json
from math import isfinite
import os
import re
from zoneinfo import ZoneInfo

from tradingagents.analysis_budget import active_budget
from tradingagents.tushare_https import HTTPS_BASE, TushareHTTPSFailure, send_once

SYMBOL = "601328.SH"
URL = HTTPS_BASE + "/daily"
FIELDS = ("ts_code", "trade_date", "close")
SH = ZoneInfo("Asia/Shanghai")


def write_new(path, data):
    with path.open("xb") as f:
        f.write(data)
        f.flush()
        os.fsync(f.fileno())


def record(path, value):
    write_new(path, json.dumps(value, ensure_ascii=False, indent=2).encode())


def unique(pairs):
    values = dict(pairs)
    if len(values) != len(pairs):
        raise ValueError("duplicate_key")
    return values


def decode(raw, target, token, *, symbol=SYMBOL):
    value = json.loads(raw, object_pairs_hook=unique)
    if not isinstance(value, dict) or type(value.get("code")) is not int or value["code"] != 0:
        raise ValueError("provider_error")
    # Only persist a narrow, credential-free successful wire envelope.
    if set(value) - {"code","msg","data","request_id"} or value.get("msg") not in (None,""):
        raise ValueError("unreviewed_metadata")
    if "request_id" in value and not re.fullmatch(r"[A-Za-z0-9_-]{1,80}", str(value["request_id"])):
        raise ValueError("unreviewed_request_id")
    if token.encode() in raw or token in json.dumps(value,ensure_ascii=False):
        raise ValueError("sensitive_response")
    table = value["data"]
    if set(table) - {"fields","items","has_more"} or table.get("has_more") not in (None,False):
        raise ValueError("partial_or_unknown_table")
    fields = table["fields"]
    if type(fields) is not list or sorted(fields) != sorted(FIELDS):
        raise ValueError("wrong_fields")
    rows = table["items"]
    if rows == []:
        raise ValueError("empty")
    if type(rows) is not list or len(rows)!=1 or type(rows[0]) is not list or len(rows[0])!=3:
        raise ValueError("ambiguous_rows")
    row = dict(zip(fields, rows[0]))
    if row["ts_code"] != symbol or row["trade_date"] != target.strftime("%Y%m%d"):
        raise ValueError("wrong_security_or_date")
    close = row["close"]
    if type(close) not in (float,int) or not isfinite(close) or close <= 0:
        raise ValueError("invalid_close")
    return close


def capture(root, target, token):
    if type(target) is not date or active_budget() is None or type(token) is not str or not token:
        raise ValueError("bounded_owner_required")
    started = datetime.now(timezone.utc)
    boundary = datetime.combine(target,time(16),SH)
    if started.astimezone(SH).date()!=target or started <= boundary:
        raise ValueError("outside_same_day_after_1600")
    dest = root / ("daily-601328-" + target.isoformat())
    dest.mkdir(exist_ok=False)
    params = {"ts_code":SYMBOL, "trade_date":target.strftime("%Y%m%d"), "ts_type_name":HTTPS_BASE}
    record(dest/"reservation.json",dict(method="POST",url=URL,params=params,fields=FIELDS,started_at=started.isoformat(),reserved=1))
    receipt = dict(status="unconfirmed",http_status=None,source=URL,observed_at=None,published_at=None)
    try:
        status, raw = send_once(URL,dict(api_name="daily",params=params,fields=",".join(FIELDS),token=token),8)
        observed = datetime.now(timezone.utc)
        receipt.update(http_status=status,observed_at=observed.isoformat())
        if status!=200 or observed<started or observed.astimezone(SH).date()!=target:
            raise ValueError("unqualified_response")
        close = decode(raw,target,token)
        write_new(dest/"response.bin",raw)
        receipt.update(status="observed_daily_close",response_sha256=sha256(raw).hexdigest(),
                       first_observed_at=observed.isoformat(),scope="auction_close_price",
                       ticker=SYMBOL,trading_date=target.isoformat(),close=close)
    except TushareHTTPSFailure as exc:
        receipt.update(status=exc.code,http_status=exc.status_code)
    except (ValueError,KeyError,TypeError,OverflowError):
        receipt["status"]="unqualified_daily_response"
    finally:
        record(dest/"receipt.json",receipt)
    return receipt

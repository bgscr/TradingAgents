"""One exchange-bound trade_cal request, only after the ordinary reader reports missing coverage."""
from datetime import datetime,timedelta
from zoneinfo import ZoneInfo
import json

CALENDAR_FAILURES = {
    "calendar_rate_limited": "交易日历接口限流；这次没有重试。",
    "calendar_https_failed": "交易日历的 HTTPS 请求失败，未获得可验证的回应。",
    "calendar_http_rejected": "交易日历接口返回了非成功的 HTTP 状态。",
    "calendar_provider_rejected": "交易日历接口返回了业务错误。",
    "calendar_response_invalid": "交易日历的返回格式或数据范围未通过核验。",
    "calendar_response_json": "交易日历未返回可解析且键唯一的 JSON。",
    "calendar_response_echo": "交易日历返回中疑似包含请求凭据，未保留正文。",
    "calendar_response_envelope": "交易日历返回的外层结构不符合约定。",
    "calendar_envelope_status": "交易日历没有返回预期的 HTTP 成功状态。",
    "calendar_envelope_root": "交易日历返回的最外层不是 JSON 对象。",
    "calendar_envelope_code_missing": "交易日历返回中缺少业务状态码。",
    "calendar_envelope_code_type": "交易日历返回的业务状态码不是整数。",
    "calendar_envelope_message": "交易日历的成功响应包含非空或不符合格式的消息。",
    "calendar_envelope_extra": "交易日历返回有未识别的外层字段。",
    "calendar_envelope_data_missing": "交易日历返回缺少数据字段。",
    "calendar_envelope_data_type": "交易日历返回的数据字段不是 JSON 对象。",
    "calendar_extra_pagination": "交易日历外层包含分页或截断标记，未采用该回复。",
    "calendar_extra_correlation_shape": "交易日历外层多出一种符合标识符格式的请求跟踪字段，未采用该回复。",
    "calendar_extra_timing_shape": "交易日历外层多出一种数字格式的时间信息字段，未采用该回复。",
    "calendar_extra_container": "交易日历外层包含未识别的对象或数组，未采用该回复。",
    "calendar_extra_unclassified": "交易日历外层包含未分类的额外字段，未采用该回复。",
    "calendar_response_fields": "交易日历返回列或列顺序不符合约定。",
    "calendar_response_pagination": "交易日历返回未确认这一页已结束。",
    "calendar_response_window_size": "交易日历未返回完整的 8 天记录。",
    "calendar_response_metadata": "交易日历返回了未核验的元数据。",
    "calendar_response_row": "交易日历记录的交易所、日期或开市标记不符合约定。",
    "calendar_response_previous_session": "交易日历记录的上一交易日不符合约定。",
}


def _extra_shape(obj, keys):
    """Return only fixed local labels, never a server-supplied key or value."""
    import math
    import re

    pagination = {
        "cursor", "hasmore", "ispartial", "limit", "next", "nextcursor",
        "nextoffset", "nextpage", "nextpagetoken", "offset", "page", "pagesize",
        "pagetoken", "pagination", "total", "totalcount", "truncated",
    }
    if any(re.sub(r"[^a-z0-9]", "", key.lower()) in pagination for key in keys):
        return "calendar_extra_pagination"
    if any(type(obj[key]) in (list, dict) for key in keys):
        return "calendar_extra_container"
    if len(keys) == 1:
        key = next(iter(keys))
        value = obj[key]
        if key in {"requestId", "trace_id", "traceId", "traceid"}:
            if type(value) is str and re.fullmatch(r"[A-Za-z0-9_-]{1,80}", value):
                return "calendar_extra_correlation_shape"
        if key in {"elapsed", "elapsed_ms", "server_time", "timestamp", "ts"}:
            if type(value) in (int, float) and 0 <= value <= 10**16 and math.isfinite(value):
                return "calendar_extra_timing_shape"
    return "calendar_extra_unclassified"


def _fetch_calendar(send, *, token, fields, exchange, unique, diagnose_extra=False):
    from tradingagents.market_history.coordinator import PhysicalAttemptFailure, PhysicalAttemptOutcome
    from tradingagents.tushare_https import TushareHTTPSFailure
    try:
        status, raw = send()
    except TushareHTTPSFailure as error:
        limited = error.status_code == 429
        delay = error.retry_after
        code = ("calendar_rate_limited" if limited else
                "calendar_http_rejected" if error.code == "http_status_rejected" else
                "calendar_https_failed")
        raise PhysicalAttemptFailure(
            outcome=PhysicalAttemptOutcome.RATE_LIMITED if limited else PhysicalAttemptOutcome.PROVIDER_ERROR,
            retryable=False, status_code=429 if limited else None, error_code=code,
            retry_after_seconds=(delay if delay and 0 < delay <= 31536000 else 300) if limited else None,
        ) from None

    def invalid(code="calendar_response_invalid"):
        return PhysicalAttemptFailure(
            outcome=(PhysicalAttemptOutcome.PROVIDER_ERROR if code == "calendar_provider_rejected"
                     else PhysicalAttemptOutcome.MALFORMED_RESPONSE),
            retryable=False, status_code=200, error_code=code,
        )

    import re
    try:
        obj = json.loads(raw, object_pairs_hook=unique)
    except (ValueError, TypeError, RecursionError):
        raise invalid("calendar_response_json") from None
    try:
        echoes_token = token in json.dumps(obj, ensure_ascii=False) or token.encode() in raw
    except (ValueError, TypeError, RecursionError, UnicodeError):
        raise invalid("calendar_response_json") from None
    if echoes_token:
        raise invalid("calendar_response_echo")
    if status != 200:
        raise invalid("calendar_envelope_status")
    if type(obj) is not dict:
        raise invalid("calendar_envelope_root")
    if "code" not in obj:
        raise invalid("calendar_envelope_code_missing")
    if type(obj["code"]) is not int:
        raise invalid("calendar_envelope_code_type")
    if obj["code"] != 0:
        raise invalid("calendar_provider_rejected")
    if obj.get("msg") not in (None, ""):
        raise invalid("calendar_envelope_message")
    extra = set(obj)-{"code","msg","data","request_id"}
    if extra:
        raise invalid(_extra_shape(obj, extra) if diagnose_extra else "calendar_envelope_extra")
    if "data" not in obj:
        raise invalid("calendar_envelope_data_missing")
    data = obj["data"]
    if type(data) is not dict:
        raise invalid("calendar_envelope_data_type")
    if data.get("fields") != fields:
        raise invalid("calendar_response_fields")
    if data.get("has_more", False) is not False:
        raise invalid("calendar_response_pagination")
    rows = data.get("items")
    if type(rows) is not list or len(rows) != 8:
        raise invalid("calendar_response_window_size")
    if set(data)-{"fields","items","has_more"} or ("request_id" in obj and not re.fullmatch(r"[A-Za-z0-9_-]{1,80}",str(obj["request_id"]))):
        raise invalid("calendar_response_metadata")
    for row in rows:
        if type(row) is not list or len(row) != 4:
            raise invalid("calendar_response_row")
        actual, cal_date, is_open, previous = row
        if actual != exchange or not isinstance(cal_date,str) or not re.fullmatch(r"[0-9]{8}",cal_date) or str(is_open) not in ("0","1"):
            raise invalid("calendar_response_row")
        if previous not in ("",None):
            if not isinstance(previous,str) or not re.fullmatch(r"[0-9]{8}",previous) or previous >= cal_date:
                raise invalid("calendar_response_previous_session")
            try:
                datetime.strptime(previous,"%Y%m%d")
            except ValueError:
                raise invalid("calendar_response_previous_session") from None
    return raw, data["items"]


def prepare(source,root,clock):
    import pandas as pd
    from tradingagents.full_session_ledger import owner,preparation_role
    from full_financial_channel import binding,write
    from full_cold_identity import token_for_identity
    from tradingagents.dataflows.bank_identity_acquisition import existing_coordinator,CaptureClock
    from tradingagents.market_history.coordinator import RequestPriority,upstream_service_identity_for_provider
    from tradingagents.dataflows.tushare_market_history import load_calendar
    from tradingagents.market_history.config import MarketHistoryConfig
    from tradingagents.market_history.store import MarketHistoryStore
    from tradingagents.market_history.current_cache import verify_prepared_sse_revision
    from tradingagents.tushare_https import HTTPS_BASE,send_once
    own=owner();intent=binding(source,root)["params"]
    if own is None or own.key!=root.name or own.params!=intent or own.path!=root/"physical-ledger"/"ledger.sqlite3" or own.child_role is not None or own.phase!="preparation" or intent["contract"]!="default-full-latest-intake-v1":
        raise ValueError("calendar_owner_mismatch")
    expected_exchange = {"XSHG": ("SSE", ".SH"), "XSHE": ("SZSE", ".SZ")}.get(intent["venue"])
    if expected_exchange is None or not intent["ticker"].endswith(expected_exchange[1]):
        raise ValueError("exchange_calendar_reader_not_qualified")
    day=datetime.now(ZoneInfo("Asia/Shanghai")).date();start=day-timedelta(days=7)
    first,last=start.strftime("%Y%m%d"),day.strftime("%Y%m%d")
    fields=["exchange","cal_date","is_open","pretrade_date"]
    params=dict(exchange=expected_exchange[0],start_date=first,end_date=last,limit=8,offset=0,ts_type_name=HTTPS_BASE)
    query=dict(api_name="trade_cal",params=params,fields=",".join(fields))
    write(root/"intake-calendar-attempt.json",dict(query=query,requested_date="latest",venue=intent["venue"],run_id=root.name))
    timer=CaptureClock();timer.end=min(timer.end,clock.deadline_monotonic);timer.end_epoch=min(timer.end_epoch,clock.effective_deadline());timer.remaining()
    uid,_=upstream_service_identity_for_provider("tushare")
    class SinglePage:
        def fetch_range(self,endpoint,*,exchange,start_date,end_date,request_executor):
            if (endpoint,exchange,start_date,end_date)!=("trade_cal",expected_exchange[0],first,last):raise ValueError("unexpected_calendar_operation")
            def send():
                token=token_for_identity(source,intent["use_project_env"])
                from daily_retention import unique
                import os
                try:
                    raw, rows = _fetch_calendar(
                        lambda: send_once(HTTPS_BASE+"/trade_cal",dict(query,token=token),min(8,timer.remaining()),coordinated_intake=True),
                        token=token, fields=fields, exchange=expected_exchange[0], unique=unique,
                        diagnose_extra=intent["mode"] == "calendar_only",
                    )
                finally:
                    del token
                with (root/"intake-calendar-response.bin").open("xb") as f:
                    f.write(raw);f.flush();os.fsync(f.fileno())
                return pd.DataFrame(rows,columns=fields)
            return request_executor(endpoint,send)
    with existing_coordinator(source) as (coord,storage),preparation_role("calendar"):
        def execute(endpoint,call):
            timer.remaining();storage.check_existing()
            result=coord.execute_direct_physical_request(request_key=root.name+":intake:trade_cal",owner_id=root.name,
                upstream_service_id=uid,priority=RequestPriority.INCREMENTAL_REFRESH,now=timer.now,sleep=timer.sleep,
                lease_duration=timedelta(seconds=35),operation="intake:trade_cal",physical_request=call,cooldown_scope="market-snapshot",record_at_physical_io=True)
            if result.physical_attempt_count!=1:raise ValueError("calendar_attempt_mismatch")
            return result.value
        pub=load_calendar(intent["ticker"],start.isoformat(),day.isoformat(),provider=SinglePage(),request_executor=execute)
    timer.remaining()
    # Run-local publication cannot displace an older, longer shared calendar revision.
    base=root/"intake-calendar"
    cfg=MarketHistoryConfig.from_mapping(dict(market_history_mode="current",data_usage_mode="personal_research",
        market_history_database_path=str(base/"market_history.sqlite3"),market_history_payload_root=str(base/"payloads"),market_history_backup_root=str(base/"backups")))
    with MarketHistoryStore.open(cfg) as store:
        rev=store.publish_session_calendar(pub).calendar_revision_id
        from full_exchange_revision import verify
        verify(store,rev,intent["ticker"],(root/"intake-calendar-response.bin").read_bytes())
        if expected_exchange[0]=="SSE":
            verify_prepared_sse_revision(store,rev,exchange_scoped=True)
    from full_financial_channel import digest
    from hashlib import sha256
    record=dict(run_id=root.name,venue=intent["venue"],revision=rev,wire_sha256=sha256((root/"intake-calendar-response.bin").read_bytes()).hexdigest())
    raw=json.dumps(record,ensure_ascii=False,sort_keys=True,separators=(",",":"),allow_nan=False).encode()
    own.register("intake_calendar",raw,digest(record),"verified-"+expected_exchange[0].lower()+"-calendar",1)
    write(root/"intake-calendar-revision.json",record)

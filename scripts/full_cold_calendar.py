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


def _has_page_control(keys, *, full=False):
    import re
    pagination = {
        "cursor", "hasmore", "ispartial", "limit", "next", "nextcursor",
        "nextoffset", "nextpage", "nextpagetoken", "offset", "page", "pagesize",
        "pagetoken", "pagination", "total", "totalcount", "truncated",
    }
    if full:
        pagination.update(("hasnext", "hasnextpage"))
    return any(re.sub(r"[^a-z0-9]", "", key.lower()) in pagination for key in keys)


def _extra_shape(obj, keys):
    """Return only fixed local labels, never a server-supplied key or value."""
    import math
    import re

    if _has_page_control(keys):
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


# CalendarOnly diagnostics are closed enums. They never authorize a response.
_SHAPES = {
    "calendar_probe_nested_page": "表层额外字段之外，数据页还带有分页或截断信号。",
    "calendar_probe_core_invalid": "额外字段之外，数据表也不符合预期的结构或行数。",
    "calendar_probe_mixed": "多个额外标量具有不同类型，需要核对供应商字段合同。",
    "calendar_probe_null": "未分类额外标量均为空。",
    "calendar_probe_boolean": "未分类额外标量均为布尔型，不能排除状态或分页含义。",
    "calendar_probe_numeric": "未分类额外标量均为数值。",
    "calendar_probe_text_empty": "未分类额外标量均为空文本。",
    "calendar_probe_text_short": "未分类额外标量均为一至八十字符文本。",
    "calendar_probe_text_other": "额外文本长度较大或长度区间不一致。",
    "calendar_probe_strict_shape": "回复通过已有解析器的形状检查，未保存或发布日历。",
}
CALENDAR_FAILURES.update(_SHAPES)


def _probe_shape(obj, keys, fields):
    # Known outer pagination/container indicators always keep precedence.
    outer = _extra_shape(obj, keys)
    data = obj.get("data")
    if outer == "calendar_extra_pagination":
        return outer
    if isinstance(data, dict) and (
        data.get("has_more", False) is not False
        or _extra_shape(data, set(data) - {"fields", "items", "has_more"}) == "calendar_extra_pagination"
    ):
        return "calendar_probe_nested_page"
    if outer != "calendar_extra_unclassified":
        return outer
    if (not isinstance(data, dict) or data.get("fields") != fields
            or not isinstance(data.get("items"), list) or len(data["items"]) != 8):
        return "calendar_probe_core_invalid"
    kinds = {type(obj[key]) for key in keys}
    if len(kinds) != 1:
        return "calendar_probe_mixed"
    kind = next(iter(kinds))
    if kind is str:
        lengths = [len(obj[key]) for key in keys]
        if max(lengths) == 0:
            return "calendar_probe_text_empty"
        if min(lengths) >= 1 and max(lengths) <= 80:
            return "calendar_probe_text_short"
        return "calendar_probe_text_other"
    return {type(None): "calendar_probe_null", bool: "calendar_probe_boolean",
            int: "calendar_probe_numeric", float: "calendar_probe_numeric"}[kind]


_COVERAGE = {
    "complete": "请求的八个自然日各有一条有效记录。",
    "missing": "请求日期有缺失。",
    "duplicate": "日期重复。",
    "outside": "日期超出窗口。",
    "exchange": "交易所不符。",
    "table": "表结构不符。",
    "date": "日期字段无效。",
    "membership": "开闭市字段无效。",
}
_PAGING = {"ended": "明确无后页。", "more": "明确有后页。", "unknown": "分页状态未知。"}
for coverage, coverage_text in _COVERAGE.items():
    for paging, paging_text in _PAGING.items():
        CALENDAR_FAILURES[f"calendar_probe_{coverage}_{paging}"] = coverage_text + paging_text


def _valid_day(value):
    if type(value) is not str or len(value) != 8 or not value.isascii() or not value.isdigit():
        return False
    try:
        datetime.strptime(value, "%Y%m%d")
    except ValueError:
        return False
    return True


def _coverage(data, fields, exchange, start):
    if not isinstance(data, dict) or data.get("fields") != fields or not isinstance(data.get("items"), list):
        return "table"
    expected = {(start + timedelta(days=i)).strftime("%Y%m%d") for i in range(8)}
    seen = set()
    for row in data["items"]:
        if not isinstance(row, list) or len(row) != 4:
            return "table"
        venue, day, is_open, previous = row
        if venue != exchange:
            return "exchange"
        if not _valid_day(day) or (previous not in (None, "") and
                                   (not _valid_day(previous) or previous >= day)):
            return "date"
        if type(is_open) not in (str, int) or str(is_open) not in ("0", "1"):
            return "membership"
        if day not in expected:
            return "outside"
        if day in seen:
            return "duplicate"
        seen.add(day)
    return "complete" if seen == expected else "missing"


def _paging(obj, data):
    if not isinstance(data, dict):
        return "unknown"
    marker = data.get("has_more")
    if marker is True:
        return "more"
    if marker is not False:
        return "unknown"
    if set(obj) - {"code", "msg", "request_id", "data"} or set(data) - {"fields", "items", "has_more"}:
        return "unknown"
    if "request_id" in obj:
        import re
        identifier = obj["request_id"]
        if type(identifier) is not str or not re.fullmatch(r"[A-Za-z0-9_-]{1,80}", identifier):
            return "unknown"
    return "ended"



def _structure(obj):
    import re
    def marker(mapping):
        if "has_more" not in mapping:
            return "absent"
        value = mapping["has_more"]
        if type(value) is bool:
            return "true" if value else "false"
        return {str:"string", int:"integer", float:"number", list:"array", dict:"object",
                type(None):"null"}[type(value)]

    root = obj if type(obj) is dict else {}
    data = root.get("data")
    data = data if type(data) is dict else {}
    identifier = root.get("request_id")
    return dict(outer_marker=marker(root), data_marker=marker(data),
                outer_extra=bool(set(root) - {"code","msg","request_id","data","has_more"}),
                data_extra=bool(set(data) - {"fields","items","has_more"}),
                request_id=("absent" if "request_id" not in root else "valid"
                            if type(identifier) is str and re.fullmatch(r"[A-Za-z0-9_-]{1,80}", identifier)
                            else "invalid"))


def _predecessors(rows):
    previous_open = None
    first = min(row[1] for row in rows)
    for _, day, flag, previous in sorted(rows, key=lambda row: row[1]):
        if not previous or (previous_open is None and previous >= first):
            return False
        if previous_open is not None and previous != previous_open:
            return False
        previous_open = day if str(flag) == "1" else previous
    return True


def _fetch_calendar(send, *, token, fields, exchange, unique, diagnose_extra=False, window_start=None, receipt=None):
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
    if receipt is not None:
        receipt(_structure(obj))
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
    if diagnose_extra:
        data = obj.get("data")
        coverage = _coverage(data, fields, exchange, window_start)
        raise invalid(f"calendar_probe_{coverage}_{_paging(obj, data)}")
    if receipt is not None:
        shape = _structure(obj)
        data = obj.get("data")
        coverage = _coverage(data, fields, exchange, window_start)
        chain = "not_checked"
        if coverage == "complete":
            chain = "consistent" if _predecessors(data["items"]) else "inconsistent"
        receipt(dict(shape, core_coverage=coverage, predecessor_chain=chain))
        # Recognized continuation fields only; unrelated extensions have no semantics here.
        if (shape["request_id"] == "invalid" or shape["data_marker"] not in {"absent", "false"}
                or _has_page_control(obj, full=True)
                or (type(data) is dict and _has_page_control(
                    (key for key in data if key != "has_more"), full=True))):
            raise invalid("calendar_response_metadata")
        if coverage != "complete":
            raise invalid("calendar_response_row")
        if chain != "consistent":
            raise invalid("calendar_response_previous_session")
        core = {"fields": fields, "items": data["items"]}
        if "has_more" in data:
            core["has_more"] = False
        evidence = json.dumps({"code": 0, "data": core}, separators=(",", ":")).encode()
        return evidence, core["items"]
    extra = set(obj)-{"code","msg","data","request_id"}
    if extra:
        raise invalid(_probe_shape(obj, extra, fields) if diagnose_extra else "calendar_envelope_extra")
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
    if diagnose_extra:
        raise invalid("calendar_probe_strict_shape")
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
    strict_full = intent["mode"] == "default_full" and intent["venue"] == "XSHG"
    if strict_full:
        params["limit"] = 9
    evidence_file = root / ("intake-calendar-core.json" if strict_full else "intake-calendar-response.bin")
    query=dict(api_name="trade_cal",params=params,fields=",".join(fields))
    if intent["mode"] == "calendar_only":
        if intent.get("session_max_calls") != 1:
            raise ValueError("invalid_calendar_diagnostic")
        attempt = dict(operation="calendar_probe", max_attempts=1)
        if "calendar_trial" in intent:
            if (intent["calendar_trial"], intent["ticker"], intent["venue"], intent["requested_date"]) != (
                    "calendar-nine-once-600919-sh-v1", "600919.SH", "XSHG", "latest"):
                raise ValueError("invalid_calendar_nine_scope")
            from full_financial_channel import digest
            from full_financial_channel import load
            claim = load(root.parent.parent / "calendar-nine-once-600919-sh-v1.json")
            if claim != dict(trial=intent["calendar_trial"], run_id=own.key):
                raise ValueError("calendar_nine_claim_mismatch")
            params["limit"] = 9
            attempt.update(run_id=root.name, expected_days=8, query=query, query_sha256=digest(query))
        write(root/"intake-calendar-attempt.json", attempt)
    else:
        write(root/"intake-calendar-attempt.json", dict(query=query,requested_date="latest",venue=intent["venue"],run_id=root.name))
    timer=CaptureClock();timer.end=min(timer.end,clock.deadline_monotonic);timer.end_epoch=min(timer.end_epoch,clock.effective_deadline());timer.remaining()
    uid,_=upstream_service_identity_for_provider("tushare")
    class SinglePage:
        def fetch_range(self,endpoint,*,exchange,start_date,end_date,request_executor):
            if (endpoint,exchange,start_date,end_date)!=("trade_cal",expected_exchange[0],first,last):raise ValueError("unexpected_calendar_operation")
            def send():
                token=token_for_identity(source,intent["use_project_env"])
                from daily_retention import unique
                import os
                shape_record = {"structure": "unavailable"}
                def receipt(shape):
                    shape_record.update(structure="decoded", **shape)
                try:
                    raw, rows = _fetch_calendar(
                        lambda: send_once(HTTPS_BASE+"/trade_cal",dict(query,token=token),min(8,timer.remaining()),coordinated_intake=True),
                        token=token, fields=fields, exchange=expected_exchange[0], unique=unique,
                        diagnose_extra=intent["mode"] == "calendar_only", window_start=start, receipt=receipt if strict_full else None,
                    )
                finally:
                    del token
                    if strict_full:
                        write(root / "intake-calendar-shape.json", dict(run_id=root.name, **shape_record))
                with evidence_file.open("xb") as f:
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
        verify(store,rev,intent["ticker"],evidence_file.read_bytes())
        if expected_exchange[0]=="SSE":
            verify_prepared_sse_revision(store,rev,exchange_scoped=True)
    from full_financial_channel import digest
    from hashlib import sha256
    record=dict(run_id=root.name,venue=intent["venue"],revision=rev)
    record["core_v1_sha256" if strict_full else "wire_sha256"] = sha256(evidence_file.read_bytes()).hexdigest()
    raw=json.dumps(record,ensure_ascii=False,sort_keys=True,separators=(",",":"),allow_nan=False).encode()
    own.register("intake_calendar",raw,digest(record),"verified-"+expected_exchange[0].lower()+"-calendar",1)
    write(root/"intake-calendar-revision.json",record)

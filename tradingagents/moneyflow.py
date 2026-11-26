"""Optional, single-request Tushare moneyflow observations, never decision facts.

The bounded launcher owns the scope. No SDK/global endpoint changes, pagination,
executor, fallback, credential persistence, or model-controlled acquisition.
"""
from __future__ import annotations

import contextlib
import contextvars
import hashlib
import http.client
import json
import os
import re
import ssl
import threading
import time
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal, localcontext
from uuid import uuid4
from zoneinfo import ZoneInfo

from tradingagents.analysis_budget import AnalysisBudgetStopped, active_budget, admitted_transport
from tradingagents.evidence import EvidenceState

VERSION = "bounded-moneyflow-v4"
LEGACY_VERSION = "bounded-moneyflow-v1"
DIAGNOSTIC_V1_VERSION = "bounded-moneyflow-v2"
DIAGNOSTIC_V2_VERSION = "bounded-moneyflow-v3"
HOST = "api.waditu.com"
PATH = "/dataapi/moneyflow"
FIELDS = ("ts_code", "trade_date", "net_mf_amount")
MAX_BYTES = 65536
MAX_JSON_DEPTH = 16
MAX_JSON_NODES = 4096
MAX_COLUMNS = 64
_SCOPE = contextvars.ContextVar("bounded_moneyflow", default=None)
_DEFERRED_MARGIN = contextvars.ContextVar("deferred_margin", default=None)
_REASONS = frozenset({"cancelled", "available", "empty", "permission_denied", "authentication",
                      "rate_limited", "http_error", "provider_error", "invalid_response",
                      "missing_values", "timeout", "transport_error", "cleanup_error",
                      "identity_invalid", "not_configured", "budget_unavailable", "upstream_busy", "historical_availability_unverified"})
_STAGES = frozenset({"request_validation", "tls_setup", "connect_send", "response_headers", "body_timeout", "body_completion",
                     "body_read", "response_limits", "decode", "response_close",
                     "connection_close", "coordinator"})
_ERROR_TYPES = frozenset({"BankIdentityError", "TimeoutError", "SSLError", "HTTPException", "OSError", "other"})
_LEGACY_REJECTIONS = frozenset({"payload_type_or_size", "json_syntax", "json_duplicate_key", "json_nonfinite",
    "envelope_type", "envelope_keys", "business_code_type", "business_status", "data_shape",
    "fields_type", "fields_name_type", "fields_duplicate", "fields_set", "items_type", "row_limit",
    "row_shape", "stock_type", "stock_mismatch", "date_format", "date_invalid", "date_window",
    "date_duplicate", "amount_missing", "amount_type", "amount_nonfinite", "amount_range",
    "amount_precision", "decoder_internal"})
_REJECTIONS = _LEGACY_REJECTIONS | {"json_limits", "fields_limit"}


class MoneyflowUnavailable(ValueError):
    """Only a closed local reason; never exception/response text."""

    def __init__(self, reason, *, retry_after=None, diagnostic=None, rejection=None):
        super().__init__(reason if type(reason) is str and reason in _REASONS else "transport_error")
        self.retry_after = retry_after
        self.diagnostic = diagnostic if _valid_diagnostic(diagnostic) else None
        self.rejection = rejection if type(rejection) is str and rejection in _REJECTIONS else None


def _reject(rejection, reason="invalid_response"):
    raise MoneyflowUnavailable(reason, rejection=rejection)


def _reason(error):
    args = BaseException.args.__get__(error)
    return args[0] if args and type(args[0]) is str and args[0] in _REASONS else "transport_error"


def _valid_details(value):
    if (type(value) is not dict or set(value) != {"chain", "truncated", "extraction_failed"}
            or type(value["chain"]) is not list or len(value["chain"]) > 4
            or type(value["truncated"]) is not bool or type(value["extraction_failed"]) is not bool):
        return False
    for node in value["chain"]:
        if (type(node) is not dict or not {"error_type"} <= set(node) <= {"error_type", "errno", "winerror"}
                or type(node["error_type"]) is not str or node["error_type"] not in _ERROR_TYPES):
            return False
        if any(type(node[key]) is not int or not -(2**31) <= node[key] <= 2**32-1
               for key in ("errno", "winerror") if key in node):
            return False
    return not value["extraction_failed"] or (not value["chain"] and not value["truncated"])


def _valid_diagnostic(value):
    if value is None:
        return True
    if type(value) is not dict or type(value.get("contract")) is not str:
        return False
    keys = {"contract", "stage", "category", "http_status", "error", "cleanup"}
    if value.get("contract") in {"moneyflow-diagnostic-v2", "moneyflow-diagnostic-v3"}:
        keys.add("rejection")
        rejection = value.get("rejection")
        allowed = _REJECTIONS if value["contract"] == "moneyflow-diagnostic-v3" else _LEGACY_REJECTIONS
        if rejection is not None and (type(rejection) is not str or rejection not in allowed
                or value.get("stage") != "decode"
                or type(value.get("category")) is not str
                or value["category"] not in {"invalid_response", "missing_values", "permission_denied", "provider_error"}):
            return False
    if (set(value) != keys
            or value["contract"] not in {"moneyflow-diagnostic-v1", "moneyflow-diagnostic-v2", "moneyflow-diagnostic-v3"}
            or type(value["stage"]) is not str or value["stage"] not in _STAGES
            or type(value["category"]) is not str or value["category"] not in (_REASONS - {"available", "empty"}) | {"cancelled"}
            or (value["http_status"] is not None and (type(value["http_status"]) is not int
                or not 100 <= value["http_status"] <= 599))
            or not _valid_details(value["error"]) or type(value["cleanup"]) is not list or len(value["cleanup"]) > 2):
        return False
    return all(type(item) is dict and set(item) == {"resource", "error"}
               and type(item["resource"]) is str and item["resource"] in {"response", "connection"}
               and _valid_details(item["error"]) for item in value["cleanup"])


def _details(error):
    # Reuse the existing bounded four-node extractor, never message/repr/traceback.
    try:
        from tradingagents.dataflows.bank_identity_acquisition import safe_error_details
        value = safe_error_details(error)
        if _valid_details(value):
            return value
    except BaseException:
        pass
    return {"chain": [], "truncated": False, "extraction_failed": True}


def _diagnostic(stage, category, error, status, *, rejection=None):
    return {"contract": "moneyflow-diagnostic-v3", "stage": stage, "category": category,
            "http_status": status if type(status) is int and 100 <= status <= 599 else None,
            "error": _details(error), "cleanup": [],
            "rejection": rejection if type(rejection) is str and rejection in _REJECTIONS and stage == "decode" else None}


def _digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":"),
                                     ensure_ascii=True, allow_nan=False).encode()).hexdigest()


def _projection_digest(api, binding, rows):
    if api == "moneyflow":
        fields = FIELDS
    elif api == "margin_detail":
        fields = ("trade_date", "ts_code", "rzye")
    else:
        raise MoneyflowUnavailable("not_configured")
    return _digest({"contract": "bounded-enhancement-projection-v1", "api": api,
                    "binding": binding, "fields": fields, "rows": rows})


def _date(value):
    if type(value) is not str or re.fullmatch(r"[0-9]{4}-[0-9]{2}-[0-9]{2}", value) is None:
        raise MoneyflowUnavailable("identity_invalid")
    return date.fromisoformat(value)


def _symbol(value):
    if type(value) is not str or re.fullmatch(r"[0-9]{6}\.(SH|SS|SZ)", value) is None:
        raise MoneyflowUnavailable("identity_invalid")
    return value[:-3] + ".SH" if value.endswith(".SS") else value


def _binding(state, ticker, as_of):
    symbol = _symbol(ticker)
    end = _date(as_of)
    if end > datetime.now(ZoneInfo("Asia/Shanghai")).date() or end < date(2010, 1, 1):
        raise MoneyflowUnavailable("identity_invalid")
    ev = EvidenceState.model_validate(state.get("evidence_state", {}))
    identity = ev.instrument_identity
    if (identity is None or not identity.is_authoritative or identity.instrument_kind.value != "equity"
            or identity.currency != "CNY" or _symbol(identity.symbol) != symbol
            or identity.venue != ("XSHG" if symbol.endswith(".SH") else "XSHE")
            or _symbol(state.get("company_of_interest")) != symbol or state.get("trade_date") != as_of):
        raise MoneyflowUnavailable("identity_invalid")
    return {"symbol": symbol, "as_of": as_of, "start": (end - timedelta(days=29)).isoformat(),
            "identity_sha256": _digest(identity.model_dump(mode="json"))}


def _amount(value):
    if type(value) not in (int, Decimal):
        _reject("amount_missing" if value is None else "amount_type",
                "missing_values" if value is None else "invalid_response")
    number = Decimal(value)
    if not number.is_finite():
        _reject("amount_nonfinite")
    if number.copy_abs() > Decimal("1e14"):
        _reject("amount_range")
    if number.as_tuple().exponent < -6:
        _reject("amount_precision")
    return format(number, "f")


def _pairs(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            _reject("json_duplicate_key")
        result[key] = value
    return result


def _json_depth(content):
    # Check nesting before json.loads allocates containers. Only JSON strings
    # suppress structural bytes; the decoder still owns all syntax validation.
    depth, quoted, escaped = 0, False, False
    for byte in content:
        if quoted:
            if escaped:
                escaped = False
            elif byte == 92:
                escaped = True
            elif byte == 34:
                quoted = False
        elif byte == 34:
            quoted = True
        elif byte in (91, 123):
            depth += 1
            if depth > MAX_JSON_DEPTH:
                _reject("json_limits")
        elif byte in (93, 125):
            depth -= 1


def _json_nodes(value):
    pending, count = [value], 0
    while pending:
        node = pending.pop()
        count += 1
        if type(node) is dict:
            count += len(node)  # Include keys, even when subsequently discarded.
            pending.extend(node.values())
        elif type(node) is list:
            pending.extend(node)
        if count + len(pending) > MAX_JSON_NODES:
            _reject("json_limits")


def _table(content, fields, row_limit):
    if type(content) is not bytes or len(content) > MAX_BYTES:
        _reject("payload_type_or_size")
    try:
        text = content.decode("utf-8")
        _json_depth(content)
        obj = json.loads(text, parse_float=Decimal, object_pairs_hook=_pairs,
                         parse_constant=lambda _: _reject("json_nonfinite"))
    except (UnicodeError, json.JSONDecodeError, RecursionError):
        _reject("json_syntax")
    _json_nodes(obj)
    if type(obj) is not dict:
        _reject("envelope_type")
    if type(obj.get("code")) is not int:
        _reject("business_code_type")
    if obj["code"] != 0:
        _reject("business_status", "permission_denied" if obj["code"] == 2002 else "provider_error")
    table = obj.get("data")
    if type(table) is not dict:
        _reject("data_shape")
    if type(table.get("fields")) is not list:
        _reject("fields_type")
    if len(table["fields"]) > MAX_COLUMNS:
        _reject("fields_limit")
    if any(type(x) is not str for x in table["fields"]):
        _reject("fields_name_type")
    if len(set(table["fields"])) != len(table["fields"]):
        _reject("fields_duplicate")
    if not set(fields) <= set(table["fields"]):
        _reject("fields_set")
    if type(table.get("items")) is not list:
        _reject("items_type")
    if len(table["items"]) > row_limit:
        _reject("row_limit")
    indexes = [table["fields"].index(name) for name in fields]
    projected = []
    for row in table["items"]:
        if type(row) is not list or len(row) != len(table["fields"]):
            _reject("row_shape")
        projected.append([row[index] for index in indexes])
    # No provider metadata, additional column name/value or digest of it escapes.
    # This is a projection, not proof that a page is complete or untruncated.
    return {"fields": list(fields), "items": projected}


def decode(content, binding):
    """Discard provider message/request-id; retain only exact typed public cells."""
    try:
        table = _table(content, FIELDS, 30)
        rows, dates = [], set()
        for row in table["items"]:
            if type(row) is not list or len(row) != 3:
                _reject("row_shape")
            cells = dict(zip(table["fields"], row, strict=True))
            d = cells["trade_date"]
            if type(cells["ts_code"]) is not str:
                _reject("stock_type")
            if cells["ts_code"] != binding["symbol"]:
                _reject("stock_mismatch")
            if type(d) is not str or re.fullmatch(r"[0-9]{8}", d) is None:
                _reject("date_format")
            try:
                d = datetime.strptime(d, "%Y%m%d").date().isoformat()
            except ValueError:
                _reject("date_invalid")
            if not binding["start"] <= d <= binding["as_of"]:
                _reject("date_window")
            if d in dates:
                _reject("date_duplicate")
            dates.add(d)
            rows.append([d, _amount(cells["net_mf_amount"])])
        return sorted(rows)
    except MoneyflowUnavailable:
        raise
    except (AnalysisBudgetStopped, KeyboardInterrupt, SystemExit):
        raise
    except Exception:
        pass
    _reject("decoder_internal")


def _send(binding, token, *, connection_factory=http.client.HTTPSConnection, clock=time.monotonic):
    """Synchronous single POST; cooperative deadline, bounded per-I/O waits/read1.

    OS DNS calls retain the existing cooperative limitation. No thread survives
    a timeout; no second send/read is attempted once a deadline/stop is observed.
    """
    budget = active_budget()
    if budget is None or budget.allowed_data_vendors != {"tushare", "akshare"}:
        raise MoneyflowUnavailable("not_configured")
    budget.check()
    if (type(binding) is not dict or set(binding) != {"symbol", "as_of", "start", "identity_sha256"}
            or _symbol(binding["symbol"]) != binding["symbol"]
            or _date(binding["as_of"]) - _date(binding["start"]) != timedelta(days=29)
            or _date(binding["as_of"]) > datetime.now(ZoneInfo("Asia/Shanghai")).date()
            or re.fullmatch(r"[a-f0-9]{64}", binding["identity_sha256"]) is None):
        raise MoneyflowUnavailable("identity_invalid")
    if type(token) is not str or not token:
        raise MoneyflowUnavailable("authentication")
    params = {"ts_code": binding["symbol"], "start_date": binding["start"].replace("-", ""),
              "end_date": binding["as_of"].replace("-", ""), "ts_type_name": "https://api.waditu.com/dataapi"}
    body = json.dumps({"api_name": "moneyflow", "token": token, "params": params,
                       "fields": ",".join(FIELDS)}, allow_nan=False).encode()
    return _post(body, PATH, connection_factory=connection_factory, clock=clock)


def _post(body, path, *, connection_factory=http.client.HTTPSConnection, clock=time.monotonic):
    """Shared bounded I/O, called only after each adapter's exact request checks.

    Two fixed HTTPS paths; no generic SDK endpoint or change to the identity /
    financial clients. The moneyflow wrapper retains its original admission.
    """
    if path not in (PATH, "/dataapi/margin_detail"):
        raise MoneyflowUnavailable("not_configured")
    budget = active_budget()
    if budget is None or budget.allowed_data_vendors != {"tushare", "akshare"}:
        raise MoneyflowUnavailable("not_configured")
    budget.check()
    connection = response = None
    active_socket = None
    failure = None
    retry_after = None
    content = None
    diagnostic = cancellation = None
    stage, status = "tls_setup", None
    deadline = clock() + min(30, budget.check())

    deferred = _DEFERRED_MARGIN.get()

    def remaining():
        if deferred is not None:
            deferred[0]()
        value = min(budget.check(), deadline - clock())
        if value <= 0:
            raise MoneyflowUnavailable("timeout")
        return value

    try:
        context = ssl.create_default_context()
        context.minimum_version = ssl.TLSVersion.TLSv1_2
        if not context.check_hostname or context.verify_mode != ssl.CERT_REQUIRED:
            raise MoneyflowUnavailable("transport_error")
        connection = connection_factory(HOST, timeout=remaining(), context=context)
        with admitted_transport("data", optional=deferred is not None) as allowance:
            if allowance is None:
                raise MoneyflowUnavailable("budget_unavailable")
            from tradingagents.market_history.coordinator import record_active_physical_attempt_io

            remaining()
            stage = "connect_send"
            record_active_physical_attempt_io()
            if deferred is not None:
                deferred[1]["io_registered"] = True
            remaining()
            if deferred is not None:
                deferred[1]["request_submission"] = "unknown"
            connection.request("POST", path, body=body,
                               headers={"Content-Type": "application/json", "Connection": "close"})
            if deferred is not None:
                deferred[1]["request_submission"] = "submitted"
            active_socket = connection.sock
            if active_socket is not None:
                active_socket.settimeout(remaining())
            stage = "response_headers"
            response = connection.getresponse()
            status = response.status
            if response.status != 200:
                if response.status == 429:
                    from tradingagents.tushare_financial_https import _retry_after_seconds

                    retry_after = _retry_after_seconds(response.getheader("Retry-After", ""))
                raise MoneyflowUnavailable({401: "authentication", 403: "permission_denied",
                                           429: "rate_limited"}.get(response.status, "http_error"))
            chunks, size = [], 0
            while True:
                stage = "body_timeout"
                timeout = remaining()
                # read1 may return the LAST NONEMPTY block and close its file,
                # releasing the transferred socket. Do not settimeout on it
                # again. A premature Content-Length EOF is still a failure.
                stage = "body_completion"
                if response.isclosed():
                    if response.length not in (None, 0):
                        raise http.client.IncompleteRead(b"")
                    break
                # HTTPConnection may hand socket ownership to the response and
                # clear connection.sock when the server closes after this body.
                stage = "body_timeout"
                if active_socket is not None:
                    active_socket.settimeout(timeout)
                stage = "body_read"
                chunk = response.read1(min(4096, MAX_BYTES + 1 - size))
                remaining()
                if not chunk:
                    if response.length not in (None, 0):
                        raise http.client.IncompleteRead(b"")
                    break
                stage = "response_limits"
                size += len(chunk)
                if size > MAX_BYTES:
                    raise MoneyflowUnavailable("invalid_response")
                chunks.append(chunk)
            content = b"".join(chunks)
    except MoneyflowUnavailable as error:
        failure = _reason(error)
        diagnostic = _diagnostic(stage, failure, error, status)
    except TimeoutError as error:
        failure = "timeout"
        diagnostic = _diagnostic(stage, failure, error, status)
    except (AnalysisBudgetStopped, KeyboardInterrupt, SystemExit) as error:
        cancellation = error
        diagnostic = _diagnostic(stage, "cancelled", error, status)
    except Exception as error:
        failure = "transport_error"
        diagnostic = _diagnostic(stage, failure, error, status)
    finally:
        for name, resource in (("response", response), ("connection", connection)):
            if resource is not None:
                try:
                    resource.close()
                except BaseException as error:
                    if isinstance(error, (AnalysisBudgetStopped, KeyboardInterrupt, SystemExit)) and cancellation is None:
                        cancellation = error
                    if diagnostic is None:
                        diagnostic = _diagnostic(name + "_close", "cleanup_error", error, status)
                    else:
                        diagnostic["cleanup"].append({"resource": name, "error": _details(error)})
                    failure = failure or "cleanup_error"
    if cancellation is not None:
        cancellation.moneyflow_diagnostic = diagnostic
        raise cancellation from None
    if failure is not None:
        raise MoneyflowUnavailable(failure, retry_after=retry_after, diagnostic=diagnostic)
    return content


def _coordinated(config, binding):
    return _coordinate(config, binding, api="moneyflow")


def _coordinate(config, binding, *, api):
    """Closed dispatch; per-interface scopes never enable the other interface."""
    if api == "moneyflow":
        enabled = config.get("bounded_moneyflow_enabled") is True
        version, fields, send, decoder = VERSION, FIELDS, _send, decode
    elif api == "margin_detail":
        from tradingagents import margin_balance

        enabled = (type(config.get("bounded_margin_date")) is str
                   and config["bounded_margin_date"] == binding.get("trade_date"))
        version, fields = margin_balance.VERSION, margin_balance.FIELDS
        send, decoder = margin_balance._send, margin_balance.decode
    else:
        raise MoneyflowUnavailable("not_configured")
    from tradingagents.dataflows.market_snapshot import record_active_physical_attempt_events
    from tradingagents.market_history.config import MarketHistoryConfig
    from tradingagents.market_history.coordinator import (
        PhysicalAttemptBudgetExhausted,
        PhysicalAttemptFailure,
        PhysicalAttemptOutcome,
        ProviderRequestCoordinator,
        RequestPriority,
        upstream_service_identity_for_provider,
    )
    from tradingagents.market_history.store import MarketHistoryStore

    budget = active_budget()
    if (budget is None or budget.allowed_data_vendors != {"tushare", "akshare"}
            or not enabled
            or config.get("tushare_account_scope_label") != "personal-2000"
            or config.get("data_usage_mode") != "personal_research" or any(config.get(key) != 40 for key in (
        "tushare_calls_per_minute", "tushare_operator_safety_ceiling_calls_per_minute"
    )) or config.get("tushare_financial_https_endpoint") != "https://api.waditu.com/dataapi"):
        raise MoneyflowUnavailable("not_configured")
    budget.check()
    if api == "margin_detail" and config.get("bounded_observation_cache_mode") == "as_known_on_date":
        raise MoneyflowUnavailable("historical_availability_unverified")
    cancellation = failure_diagnostic = None

    def query():
        nonlocal cancellation, failure_diagnostic
        budget.check()
        started_at = datetime.now(timezone.utc)
        stage = "request_validation"
        try:
            # Exactly the launcher's already-authorized in-memory credential.
            content = send(binding, os.environ.get("TUSHARE_TOKEN"))
            received_at = datetime.now(timezone.utc).isoformat()
            stage = "decode"
            rows = decoder(content, binding)
            # An in-flight duplicate may reuse this result. Preserve the owner
            # request's receipt time, never relabel it as the follower's fetch.
            return rows, _projection_digest(api, binding, rows), received_at
        except MoneyflowUnavailable as error:
            reason = _reason(error)
            failure_diagnostic = error.diagnostic or _diagnostic(stage, reason, error,
                200 if stage == "decode" else None, rejection=error.rejection)
            mapping = {"permission_denied": PhysicalAttemptOutcome.PERMISSION_DENIED,
                       "authentication": PhysicalAttemptOutcome.AUTHENTICATION,
                       "rate_limited": PhysicalAttemptOutcome.RATE_LIMITED,
                       "timeout": PhysicalAttemptOutcome.TIMEOUT,
                       "invalid_response": PhysicalAttemptOutcome.MALFORMED_RESPONSE}
            raise PhysicalAttemptFailure(outcome=mapping.get(reason, PhysicalAttemptOutcome.PROVIDER_ERROR),
                                         retryable=False, error_code=api + "_" + reason,
                                         retry_after_seconds=(
                                             (300 if error.retry_after is None else error.retry_after)
                                             + max(0, (datetime.now(timezone.utc) - started_at).total_seconds())
                                             if reason == "rate_limited" else None)) from None
        except (AnalysisBudgetStopped, KeyboardInterrupt, SystemExit) as error:
            # The shared coordinator records BaseException as a physical failure.
            # Keep cancellation outside that conversion; never continue to a model.
            cancellation = error
            raise

    def sleep(seconds):
        deferred = _DEFERRED_MARGIN.get()
        if deferred is None:
            time.sleep(min(seconds, budget.check()))
        else:
            until = time.monotonic() + seconds
            while (left := until - time.monotonic()) > 0:
                deferred[0]()
                time.sleep(min(left, budget.check(), 0.1))
        budget.check()

    with MarketHistoryStore.open_provider_request_authority(MarketHistoryConfig.from_mapping(config)) as store:
        coordinator = ProviderRequestCoordinator(store)
        upstream_id, service = upstream_service_identity_for_provider("tushare")
        coordinator.register_upstream_service(upstream_id, service)
        request_key = api + ":" + _digest({"contract": version, "binding": binding,
            "fields": fields, "destination": HOST + "/dataapi/" + api,
            "account_scope": config["tushare_account_scope_label"]})
        if api == "margin_detail" and config.get("bounded_observation_cache_mode") == "retrospective":
            from tradingagents.retrospective_observations import retrospective_observation

            cached = retrospective_observation(store.config,
                request_key=request_key, provider="tushare", operation=api, as_of=_date(binding["as_of"]),
                receipt_time=lambda value: margin_balance.cached_receipt_time(value, binding),
            )
            if cached is not None:
                budget.check()
                return cached
        coordinator.configure_operator_ceiling(upstream_service_id=upstream_id,
                                               minimum_interval=timedelta(seconds=1.5),
                                               allow_prewarming=False, configured_at=datetime.now(timezone.utc))
        try:
            try:
                result = coordinator.execute_physical_workflow(
                    request_key=request_key, upstream_service_id=upstream_id,
                    owner_id=api + ":" + uuid4().hex, priority=RequestPriority.INTERACTIVE_MAINLAND,
                    now=lambda: datetime.now(timezone.utc), sleep=sleep, lease_duration=timedelta(minutes=2),
                    max_physical_operations=1, wait_for_initial_pacing=True,
                    workflow=lambda execute: execute(api, query), cooldown_scope="market-snapshot")
            except PhysicalAttemptBudgetExhausted as error:
                record_active_physical_attempt_events(error.attempt_events)
                reason = (error.failure.error_code or "").removeprefix(api + "_")
                raise MoneyflowUnavailable(reason if reason in _REASONS else "upstream_busy",
                                           diagnostic=failure_diagnostic) from None
            record_active_physical_attempt_events(result.attempt_events)
            return result.value
        finally:
            if cancellation is not None:
                raise cancellation from None


class _Run:
    def __init__(self, config, ticker, as_of):
        self.config, self.ticker, self.as_of = dict(config), _symbol(ticker), as_of
        _date(as_of)
        self.used, self.result = False, None
        self.lock = threading.Lock()

    def acquire(self, state):
        with self.lock:
            binding = _binding(state, self.ticker, self.as_of)
            if self.used:
                if self.result is None:
                    raise AnalysisBudgetStopped("moneyflow_attempt_already_started")
                if any(self.result.get(k) != v for k, v in binding.items()):
                    raise AnalysisBudgetStopped("moneyflow_binding_changed")
                return json.loads(json.dumps(self.result))
            if state.get("moneyflow_observation") is not None:
                raise AnalysisBudgetStopped("moneyflow_restore_unsupported")
            self.used = True
            budget = active_budget()
            if budget is None:
                raise MoneyflowUnavailable("not_configured")
            budget.check()
            before = budget.receipt()["data_attempts"]
            rows, projection_sha, status = [], None, "available"
            diagnostic = None
            received_at = None
            try:
                rows, projection_sha, received_at = _coordinated(self.config, binding)
                if not rows:
                    status = "empty"
            except MoneyflowUnavailable as error:
                status = _reason(error)
                diagnostic = error.diagnostic
            budget.check()
            result = {"contract": VERSION, **binding, "provider": "tushare.moneyflow", "unit": "CNY_10000",
                      "received_at": received_at, "provenance": "retrospective_backfill",
                      "status": status, "rows": rows, "projected_data_sha256": projection_sha,
                      "attempts": budget.receipt()["data_attempts"] - before, "diagnostic": diagnostic}
            result["normalized_sha256"] = _digest(result)
            self.result = result
            return json.loads(json.dumps(result))


@contextlib.contextmanager
def moneyflow_scope(config, *, ticker, as_of):
    if _SCOPE.get() is not None:
        raise MoneyflowUnavailable("not_configured")
    token = _SCOPE.set(_Run(config, ticker, as_of))
    try:
        yield
    finally:
        _SCOPE.reset(token)


def moneyflow_for_state(state):
    run = _SCOPE.get()
    return None if run is None else run.acquire(state)


def _window_totals_markdown(checked, binding, chinese):
    """Derived only after the observation's existing binding/row checks passed."""
    recent_start = (date.fromisoformat(binding['as_of']) - timedelta(days=7)).isoformat()
    lines = (["### 资金流窗口合计（确定性复算）", "单位：万元人民币；字段：Tushare net_mf_amount。",
              "| 窗口 | 查询起止 | 实际记录起止 | 合格交易日记录数 | 净额合计 |",
              "|---|---|---|---:|---:|"] if chinese else
             ["### Flow window totals (deterministic calculation)", "Unit: CNY 10,000; field: Tushare net_mf_amount.",
              "| Window | Requested range | Actual row range | Qualified daily rows | Net total |",
              "|---|---|---|---:|---:|"])
    for label, start in (("全采集窗口" if chinese else "Acquisition window", binding['start']),
                         ("情绪回看窗口" if chinese else "Sentiment lookback window", recent_start)):
        rows = [row for row in checked if start <= row[0] <= binding['as_of']]
        with localcontext() as context:
            context.prec = 40
            total = format(sum((Decimal(row[1]) for row in rows), Decimal(0)), 'f') if rows else ('不可用' if chinese else 'unavailable')
        actual = f'{rows[0][0]} → {rows[-1][0]}' if rows else '—'
        lines.append(f"| {label} | {start} → {binding['as_of']} | {actual} | {len(rows)} | {total} |")
    lines.append('记录数不是完整交易日覆盖证明；空窗口不可用，不补零。合计、起止和记录数必须整组引用，不得跨窗口挪用。'
                 if chinese else 'Counts do not prove complete session coverage; empty windows are unavailable, not zero. Quote each total together with its range and count; never transfer totals between windows.')
    lines.append('来源绑定及投影摘要见下方；模型正文未经过逐句窗口／合计核验，不得据此升级为核验事实。'
                 if chinese else 'Source binding and projection digest follow. Model prose is not checked claim-by-claim for window/total agreement and cannot become validated facts.')
    return '\n'.join(lines)


def moneyflow_markdown(state, *, language="Chinese", include_rows=True):
    value = state.get("moneyflow_observation")
    if value is None:
        return ""
    chinese = language.lower() in {"chinese", "zh", "zh-cn", "中文"}
    invalid = "资金流增强：材料绑定无效，未采用。" if chinese else "Moneyflow: invalid binding; excluded."
    try:
        binding = _binding(state, state.get("company_of_interest"), state.get("trade_date"))
        current = type(value) is dict and value.get("contract") == VERSION
        digest_key = "projected_data_sha256" if current else "raw_response_sha256"
        expected = {"contract", *binding, "provider", "unit", "received_at", "provenance", "status", "rows",
                    digest_key, "attempts", "normalized_sha256"}
        if type(value) is dict and value.get("contract") in {VERSION, DIAGNOSTIC_V1_VERSION, DIAGNOSTIC_V2_VERSION}:
            expected.add("diagnostic")
        if (type(value) is not dict or set(value) != expected or value["contract"] not in {VERSION, LEGACY_VERSION, DIAGNOSTIC_V1_VERSION, DIAGNOSTIC_V2_VERSION}
                or any(value[k] != v for k, v in binding.items())
                or value["normalized_sha256"] != _digest({k: v for k, v in value.items() if k != "normalized_sha256"})
                or value["status"] not in _REASONS or value["provider"] != "tushare.moneyflow"
                or value["unit"] != "CNY_10000" or value["provenance"] != "retrospective_backfill"
                or type(value["attempts"]) is not int or value["attempts"] not in (0, 1)
                or not _valid_diagnostic(value.get("diagnostic"))):
            return invalid
        detail = value.get("diagnostic")
        expected_diagnostic = {VERSION: "moneyflow-diagnostic-v3", DIAGNOSTIC_V2_VERSION: "moneyflow-diagnostic-v2",
                               DIAGNOSTIC_V1_VERSION: "moneyflow-diagnostic-v1"}
        if detail is not None and detail["contract"] != expected_diagnostic.get(value["contract"]):
            return invalid
        if value["received_at"] is not None:
            received = datetime.fromisoformat(value["received_at"])
            if received.utcoffset() != timedelta(0) or received > datetime.now(timezone.utc) + timedelta(seconds=1):
                return invalid
        if current:
            if value["status"] in {"available", "empty"}:
                if value[digest_key] != _projection_digest("moneyflow", binding, value["rows"]):
                    return invalid
            elif value[digest_key] is not None or value["received_at"] is not None:
                return invalid
        if value["status"] != "available":
            if value["rows"]:
                return invalid
            detail = value.get("diagnostic")
            if detail is not None and detail["category"] != value["status"]:
                return invalid
            diagnostic_text = ""
            if detail is not None:
                label = "\n\n安全诊断（非投资证据）：" if chinese else "\n\nSafe diagnostic (not investment evidence): "
                diagnostic_text = label + json.dumps(detail, ensure_ascii=True)
            return ("## 资金流增强（辅助观察）\n\n未取得合格资金流；状态：" if chinese else
                    "## Moneyflow (advisory)\n\nNo qualified moneyflow; status: ") + value["status"] + (
                        "。不代表净流入为零，不改变评级。" if chinese else ". Not zero flow; rating unchanged.") + diagnostic_text
        if value.get("diagnostic") is not None:
            return invalid
        if (not value["rows"] or value["received_at"] is None
                or not re.fullmatch(r"[a-f0-9]{64}", value[digest_key] or "")
                or len(value["rows"]) > 30):
            return invalid
        checked, seen = [], set()
        for row in value["rows"]:
            if type(row) is not list or len(row) != 2 or type(row[1]) is not str or len(row[1]) > 30:
                return invalid
            day = _date(row[0]).isoformat()
            if not binding["start"] <= day <= binding["as_of"] or day in seen:
                return invalid
            seen.add(day)
            checked.append((day, _amount(Decimal(row[1]))))
        if checked != sorted(checked):
            return invalid
        with localcontext() as context:
            context.prec = 40
            total = format(sum((Decimal(row[1]) for row in checked), Decimal(0)), "f")
        header = "## 资金流增强（辅助观察，不改变评级）" if chinese else "## Moneyflow (advisory; rating unchanged)"
        intro = (f"股票：{binding['symbol']}；查询区间：{binding['start']} 至 {binding['as_of']}（30个自然日）。\n"
                 f"收到 {len(checked)} 条合格日记录；仅这些记录的净流入额合计：{total} 万元。\n"
                 "口径为 Tushare net_mf_amount 主动买卖单净值，非主力持仓变化、非未来收益预测。\n"
                 "未证明全部交易日覆盖；缺行不补零，同日数据可能尚未发布完整。\n"
                 "这是事后取得的历史观察（retrospective_backfill），不证明当时可得，不用于严格时点回测。\n"
                 + ("仅保留校验后的数据投影及其摘要；额外元数据未留存或哈希，未证明响应全量。" if current else
                    "旧版记录保留规范化数字与原响应摘要；原始响应字节未保留。")) if chinese else (
                     f"Symbol: {binding['symbol']}; requested {binding['start']} through {binding['as_of']} (30 calendar days).\n"
                     f"{len(checked)} qualified daily rows; sum over returned rows only: {total} CNY 10,000.\n"
                     "Tushare net_mf_amount: active-order net flow, not holdings changes or a return prediction.\n"
                     "Trading-session completeness unproven; missing rows are not zero. Same-day publication may be incomplete.\n"
                     "Retrospective backfill (retrospective_backfill), not proof of historical availability or strict PIT replay.\n"
                     + ("Only validated projection and its digest retained; extra metadata neither retained nor hashed. Completeness unproven."
                        if current else "Legacy observation retains normalized rows and raw-response digest, not original bytes."))
        return (header + "\n\n" + intro + '\n\n' + _window_totals_markdown(checked, binding, chinese)
                + f"\n\nSource: https://tushare.pro/document/2?doc_id=170\nReceived UTC: {value['received_at']}\n"
                + (f"本会话新增发送：{value['attempts']}（0 表示复用并发请求结果，保留原收到时间）。\n" if chinese else
                   f"New sends in this run: {value['attempts']} (0 means coalesced in-flight result; original receipt time retained).\n")
                + f"{'Validated projection' if current else 'Raw response'} SHA256: {value[digest_key]}\nNormalized observation SHA256: {value['normalized_sha256']}\n"
                + ("\n| Date | net_mf_amount (CNY 10,000) |\n|---|---:|\n" +
                   "\n".join(f"| {day} | {amount} |" for day, amount in checked) if include_rows else ""))
    except Exception:
        return invalid

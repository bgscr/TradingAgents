"""One supplied response through the current production news route and parser.

No default live transport. Capture needs separate caller authorization. Only
conservatively validated public bytes may be retained. Offline replay establishes
neither supplier qualification nor historical availability.
"""
from __future__ import annotations

import copy
import errno
import hashlib
import json
import re
import socket
import ssl
import stat
from contextlib import suppress
from itertools import islice
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from requests import exceptions as request_errors
from urllib3 import exceptions as pool_errors

from tradingagents import run_diagnostics
from tradingagents.dataflows import (
    acquisition,
    akshare_data,
    config,
    eastmoney_news,
    interface,
    market_snapshot,
)
from tradingagents.default_config import DEFAULT_CONFIG

CONTRACT = "news-production-diagnostic-v4"
METADATA_CONTRACT = "news-live-metadata-only-v1"
# These are diagnostic targets, not identity or live-execution authority.
TICKERS = frozenset({"600895.SH", "600919.SH"})
URL = "https://search-api-web.eastmoney.com/search/jsonp"
LIMIT = 1024 * 1024
COUNT_LIMIT = 65535
BODY_NAME = "public-response.bin"
PUBLIC_FIELDS = {"date", "mediaName", "code", "title", "content", "image"}
PUBLIC_COUNTS = {"code", "hits", "total", "count"}
STRUCTURE_LABELS = PUBLIC_FIELDS | PUBLIC_COUNTS | {
    "result", "cmsArticleWebOld", "success", "status", "error", "message", "msg", "version", "type", "totalCount",
}
EXCLUSIONS = ("malformed", "undated", "outside_window", "wrong_stock")
PARSE_REASONS = frozenset({
    "body_type", "body_limit", "expected_callback_invalid", "encoding_invalid",
    "callback_invalid", "wrapper_invalid", "duplicate_json_key", "nonfinite_json_number",
    "json_invalid", "trailing_syntax", "root_not_object", "business_failure",
    "business_status_unverified", "request_invalid", "schema_invalid", "no_usable_articles",
})
SENSITIVE_TEXT = eastmoney_news.SENSITIVE_TEXT
_EXCEPTION_TYPES = frozenset({
    "ValueError", "TypeError", "UnicodeError", "UnicodeDecodeError", "OSError",
    "PermissionError", "FileExistsError", "FileNotFoundError", "TimeoutError",
    "ConnectionError", "Timeout", "ReadTimeout", "ConnectTimeout", "HTTPError",
    "AcquisitionFailure", "NewsResponseError", "NewsResponseRejected", "RuntimeError", "SSLError", "ProxyError",
})

TRANSPORT_ERROR_CONTRACT = "news-transport-error-v1"
_CHAIN_LIMIT = 8
_ARGUMENT_LIMIT = 4
_REFERENCE_FIELD_LIMIT = 8
# Identity, not class-name/MRO matching: unknown subclasses cannot impersonate
# a trusted library class or execute arbitrary attribute/formatting hooks.
_TRANSPORT_TYPES = (
    (request_errors.ConnectionError, "requests.ConnectionError", "connection"),
    (request_errors.ProxyError, "requests.ProxyError", "proxy"),
    (request_errors.SSLError, "requests.SSLError", "tls"),
    (request_errors.ConnectTimeout, "requests.ConnectTimeout", "connect_timeout"),
    (request_errors.ReadTimeout, "requests.ReadTimeout", "read_timeout"),
    (request_errors.Timeout, "requests.Timeout", "timeout"),
    (request_errors.RetryError, "requests.RetryError", "transport_wrapper"),
    (request_errors.ChunkedEncodingError, "requests.ChunkedEncodingError", "protocol"),
    (request_errors.RequestException, "requests.RequestException", "transport_wrapper"),
    (pool_errors.MaxRetryError, "urllib3.MaxRetryError", "transport_wrapper"),
    (pool_errors.NewConnectionError, "urllib3.NewConnectionError", "connection"),
    (pool_errors.NameResolutionError, "urllib3.NameResolutionError", "dns"),
    (pool_errors.ConnectTimeoutError, "urllib3.ConnectTimeoutError", "connect_timeout"),
    (pool_errors.ReadTimeoutError, "urllib3.ReadTimeoutError", "read_timeout"),
    (pool_errors.ProxyError, "urllib3.ProxyError", "proxy"),
    (pool_errors.SSLError, "urllib3.SSLError", "tls"),
    (pool_errors.ProtocolError, "urllib3.ProtocolError", "protocol"),
    (pool_errors.ClosedPoolError, "urllib3.ClosedPoolError", "pool"),
    (OSError, "builtins.OSError", "os_error"),
    (ConnectionError, "builtins.ConnectionError", "connection"),
    (ConnectionRefusedError, "builtins.ConnectionRefusedError", "connection_refused"),
    (ConnectionResetError, "builtins.ConnectionResetError", "connection_reset"),
    (ConnectionAbortedError, "builtins.ConnectionAbortedError", "connection_aborted"),
    (TimeoutError, "builtins.TimeoutError", "timeout"),
    (PermissionError, "builtins.PermissionError", "permission"),
    (BrokenPipeError, "builtins.BrokenPipeError", "broken_pipe"),
    (socket.gaierror, "socket.gaierror", "dns"),
    (ssl.SSLError, "ssl.SSLError", "tls"),
    (ssl.SSLCertVerificationError, "ssl.SSLCertVerificationError", "tls_certificate"),
    (ssl.SSLEOFError, "ssl.SSLEOFError", "tls"),
)
_WRAPPER_TYPES = tuple(row[0] for row in _TRANSPORT_TYPES if row[1].startswith(("requests.", "urllib3.")))
_OS_TYPES = tuple(row[0] for row in _TRANSPORT_TYPES if not row[1].startswith(("requests.", "urllib3.")))
_WINERRORS = frozenset({5, 10013, 10048, 10049, 10050, 10051, 10052, 10053,
                       10054, 10057, 10060, 10061, 10064, 10065, 11001, 11002, 11003, 11004})
_OS_ERRNOS = frozenset({errno.EPERM, errno.EACCES, errno.ECONNREFUSED, errno.ECONNRESET,
                       errno.ECONNABORTED, errno.ETIMEDOUT, errno.EHOSTUNREACH, errno.ENETUNREACH,
                       errno.ENETDOWN, errno.ENOTCONN, errno.EPIPE, errno.EADDRNOTAVAIL,
                       errno.EADDRINUSE, errno.EINVAL}) | (_WINERRORS - {5})
_DNS_ERRNOS = frozenset(value for name in (
    "EAI_AGAIN", "EAI_NONAME", "EAI_FAIL", "EAI_NODATA", "EAI_SYSTEM", "EAI_BADFLAGS",
    "EAI_FAMILY", "EAI_SERVICE", "EAI_SOCKTYPE", "EAI_MEMORY", "EAI_OVERFLOW",
) if type(value := getattr(socket, name, None)) is int)
_TLS_ERRNOS = frozenset({ssl.SSL_ERROR_SSL, ssl.SSL_ERROR_WANT_READ, ssl.SSL_ERROR_WANT_WRITE,
                        ssl.SSL_ERROR_SYSCALL, ssl.SSL_ERROR_ZERO_RETURN, ssl.SSL_ERROR_EOF})


def _exception_reference(value):
    # Inspect the actual type, never value.__class__ or an arbitrary property.
    return issubclass(type(value), BaseException)


def _next_transport_error(error, known):
    cause = BaseException.__cause__.__get__(error)
    if cause is not None:
        return cause, "cause", None
    reference_unavailable = False
    for cls, key in ((pool_errors.MaxRetryError, "reason"),
                     (pool_errors.ProxyError, "original_error"),
                     (pool_errors.NameResolutionError, "_reason")):
        if type(error) is cls:
            fields = object.__getattribute__(error, "__dict__")
            if type(fields) is not dict:
                reference_unavailable = True
                continue
            found = False
            # No overridable dict methods or hash lookups: even an ordinary
            # dict can contain a hostile non-string key with __eq__ hooks.
            for name, value in islice(dict.items(fields), _REFERENCE_FIELD_LIMIT):
                if type(name) is str and name == key:
                    found = True
                    if _exception_reference(value):
                        return value, "reason", None
                    break
            reference_unavailable = not found and len(fields) > _REFERENCE_FIELD_LIMIT
    arguments_limited = False
    if any(type(error) is cls for cls in _WRAPPER_TYPES):
        arguments = BaseException.args.__get__(error)
        arguments_limited = len(arguments) > _ARGUMENT_LIMIT
        for value in arguments[:_ARGUMENT_LIMIT]:
            if _exception_reference(value):
                return value, "argument", None
    if not BaseException.__suppress_context__.__get__(error):
        context = BaseException.__context__.__get__(error)
        if context is not None:
            return context, "context", None
    return None, None, ("reference_unavailable" if reference_unavailable else
                        "argument_limit" if arguments_limited else "no_link" if known else "unknown_type")


def _transport_error_detail(error):
    """One bounded selected chain, never messages, traceback or arbitrary data.

    Link priority: explicit cause, known wrapper reason, exception argument,
    unsuppressed context. Labels describe observations, not historical root cause
    or the number of physical attempts (MaxRetryError can occur with zero retries).
    """
    detail = {"contract": TRANSPORT_ERROR_CONTRACT, "chain": [], "termination": "unavailable"}
    seen, via = set(), "root"
    while _exception_reference(error):
        if id(error) in seen:
            detail["termination"] = "cycle"
            break
        if len(detail["chain"]) == _CHAIN_LIMIT:
            detail["termination"] = "node_limit"
            break
        seen.add(id(error))
        known = next((row for row in _TRANSPORT_TYPES if type(error) is row[0]), None)
        node = {"type": known[1] if known else "other", "category": known[2] if known else "unknown",
                "via": via, "errno": None, "winerror": None}
        if any(type(error) is cls for cls in _OS_TYPES):
            allowed = (_DNS_ERRNOS if type(error) is socket.gaierror else
                       _TLS_ERRNOS if known[2] in {"tls", "tls_certificate"} else _OS_ERRNOS)
            code = OSError.errno.__get__(error)
            if type(code) is int and code in allowed:
                node["errno"] = code
            descriptor = getattr(OSError, "winerror", None)
            code = descriptor.__get__(error) if descriptor is not None else None
            if type(code) is int and code in _WINERRORS:
                node["winerror"] = code
        detail["chain"].append(node)
        error, via, termination = _next_transport_error(error, known is not None)
        if error is None:
            detail["termination"] = termination
            break
    return detail


def _article_problem(rows):
    if not isinstance(rows, list):
        return "articles_not_list"
    if len(rows) > 10:
        return "article_limit"
    for row in rows:
        if (not isinstance(row, dict)
                or not {"date", "mediaName", "code", "title", "content"} <= set(row) <= PUBLIC_FIELDS):
            return "unknown_or_missing_article_fields"
        if any(not isinstance(value, str) for value in row.values()):
            return "non_string_article_field"
        if any(SENSITIVE_TEXT.search(value) for value in row.values()):
            return "sensitive_article_text"
    return None


def _metadata_shape(body, value, matches):
    """Describe fixed syntax only, never a payload projection or admission.

    A dict/bool pair is supplied only by the existing strict JSONP decoder.
    Otherwise inspect plain JSON using the same duplicate/nonfinite rejection.
    Do not recurse into unknown fields or retain any actual callback or value.
    """
    available = type(value) is dict and type(matches) is bool
    wrapper = ("expected_jsonp" if matches else "other_valid_jsonp") if available else "invalid_or_unknown"
    if not available and type(body) is bytes and len(body) <= LIMIT:
        try:
            value = json.loads(body.decode("utf-8-sig"), object_pairs_hook=eastmoney_news._pairs,
                               parse_constant=eastmoney_news._constant, parse_float=eastmoney_news._float)
        except (ValueError, UnicodeError, RecursionError):
            value = None
        else:
            available, wrapper = True, "plain_json"
    missing = object()
    root = value if available else missing
    type_names = {str: "string", bool: "boolean", int: "integer", float: "number",
                  dict: "object", list: "array", type(None): "null"}
    known = ("code", "msg", "bizCode", "bizMsg", "result", "data", "error", "hitsTotal",
             "cmsArticleWebOld", "status", "success")
    scopes = {}
    # Exactly root and its two named children; never unknown names or paths.
    for label, obj in (("root", root),
                       ("result", root.get("result", missing) if type(root) is dict else missing),
                       ("data", root.get("data", missing) if type(root) is dict else missing)):
        is_object = type(obj) is dict
        fields = {key: (type_names.get(type(obj[key]), "other") if key in obj else "absent")
                  if is_object else "not_examined" for key in known}
        code = obj.get("code", missing) if is_object else missing
        code_class = ("not_examined" if not is_object else "absent" if code is missing else
                      "integer_zero" if type(code) is int and code == 0 else
                      "integer_nonzero" if type(code) is int else "wrong_type")
        messages = {}
        for key in ("msg", "bizMsg"):
            message = obj.get(key, missing) if is_object else missing
            messages[key] = ("not_examined" if not is_object else "absent" if message is missing else
                             "wrong_type" if type(message) is not str else
                             "exact_OK" if message == "OK" else "empty_string" if message == "" else "unknown")
        rows = obj.get("cmsArticleWebOld") if is_object else None
        scopes[label] = {
            "container_type": ("unavailable" if not available else "absent" if obj is missing else
                               type_names.get(type(obj), "other")),
            "fields": fields, "code_class": code_class, "message_classes": messages,
            "article_count": min(len(rows), COUNT_LIMIT) if type(rows) is list else None,
            "count_capped": type(rows) is list and len(rows) > COUNT_LIMIT,
        }
    return {"contract": "news-closed-shape-v1", "classification": "observed" if available else "unavailable",
            "wrapper": wrapper, "qualification_authority": False, "scopes": scopes}


_PUBLIC_RESULT_NAMES = frozenset(STRUCTURE_LABELS | {
    "data", "bizCode", "bizMsg", "hitsTotal", "cmsArticleWeb", "passportWeb",
    "stock", "stocks", "fund", "funds", "news", "articles", "list", "items",
})
_PUBLIC_NAME = re.compile(r"[A-Za-z][A-Za-z0-9_]{0,39}")
_PRIVATE_NAME = re.compile(r"credential|token|session|authorization|cookie|password|secret|api_?key|access_?key|private_?key|jwt|bearer", re.I)
_PUBLIC_CALLBACK = re.compile(r"(jQuery[0-9]{6,25}_([0-9]{13}))\s*\(")


def _unavailable_public_structure():
    return {"contract": "news-public-structure-v1", "public_request_binding": "unverified",
            "qualification_authority": False,
            "callback": {"status": "unavailable", "name": None},
            "result": {"status": "unavailable", "fields": [], "inspection_complete": False,
                       "field_count": None, "count_capped": False, "hidden_counts": {
                           "sensitive_name": 0, "name_not_reviewed": 0, "name_format": 0,
                           "unverified_scalar_value": 0}}}


def _public_structure_metadata(body, value, matches, public_request_verified):
    """Opt-in public correlation/field metadata, never values or admission.

    The caller must positively bind the actual unauthenticated PreparedRequest.
    The strict decoder must already have accepted the entire JSONP wrapper.
    Common jQuery naming is correlation syntax, not source/secret certification.
    Nested containers are not traversed or claimed to be content-screened.
    """
    view = _unavailable_public_structure()
    if public_request_verified is not True:
        return view
    view["public_request_binding"] = "verified"
    if type(body) is not bytes or len(body) > LIMIT or type(value) is not dict or type(matches) is not bool:
        return view
    # Decode only the bounded prefix here; the full strict decoder has already
    # accepted it. No arbitrary callback or prefix is copied to the receipt.
    prefix = body[:128].decode("utf-8-sig", errors="ignore").lstrip()
    match = _PUBLIC_CALLBACK.match(prefix)
    if match and 946684800000 <= int(match[2]) <= 4102444800000:
        view["callback"] = {"status": "public_jquery_identifier", "name": match[1]}
    else:
        view["callback"]["status"] = "hidden_format_or_semantics"
    result = view["result"]
    if "result" not in value:
        result["status"] = "absent"
        return view
    obj = value["result"]
    if type(obj) is not dict:
        result["status"] = "wrong_type"
        return view
    result.update(field_count=min(len(obj), 32), count_capped=len(obj) > 32)
    if len(obj) > 32:
        result["status"] = "field_limit"
        return view
    kinds = {dict: "object", list: "array", str: "string", bool: "boolean",
             int: "integer", float: "number", type(None): "null"}
    for name, item in obj.items():
        hidden = ("name_format" if type(name) is not str or not _PUBLIC_NAME.fullmatch(name) else
                  "sensitive_name" if _PRIVATE_NAME.search(name) else
                  "name_not_reviewed" if name not in _PUBLIC_RESULT_NAMES else
                  "unverified_scalar_value" if type(item) not in kinds or (type(item) is str and item) else None)
        if hidden:
            result["hidden_counts"][hidden] += 1
            continue
        emptiness = ("nonempty" if len(item) else "empty") if type(item) in (dict, list, str) else "not_applicable"
        result["fields"].append({"name": name, "type": kinds[type(item)], "emptiness": emptiness})
    result.update(status="observed", inspection_complete=True)
    return view


def _public_payload(body, callback):
    # Shared conservative policy; the manual path retains only its validated
    # projection, never the user's raw clipboard/console text.
    problem, rows, value, callback_matches = eastmoney_news.public_news_payload(body, callback)
    if value is None:
        return problem, rows, value, callback_matches
    # Reuse only closed categories; classification cannot replace the actual
    # retention result. DiagnosticPersistenceError (BaseException) stays terminal.
    with suppress(Exception):
        run_diagnostics.record_news_business_status(value, value.get('result'), stage="decode")
    return problem, rows, value, callback_matches


def _safe_structure(value):
    """Bounded types/counts, never values or unknown literal field names."""
    def fields(obj):
        kinds = {dict: "object", list: "array", str: "string", bool: "boolean",
                 int: "number", float: "number", type(None): "null"}
        if not isinstance(obj, dict):
            return {"type": kinds.get(type(obj), "other")}
        items = []
        for name, item in list(obj.items())[:32]:
            entry = {"type": kinds.get(type(item), "other")}
            if name in STRUCTURE_LABELS:
                entry["name"] = name
            else:
                entry["name_sha256"] = hashlib.sha256(name.encode("utf-8", errors="surrogatepass")).hexdigest()
                entry["name_length"] = min(len(name), COUNT_LIMIT)
            if isinstance(item, (dict, list, str)):
                entry["size"] = min(len(item), COUNT_LIMIT)
            items.append(entry)
        return {"type": "object", "field_count": min(len(obj), COUNT_LIMIT),
                "fields": items, "truncated": len(obj) > 32}

    result = {"envelope": fields(value)}
    if isinstance(value, dict) and isinstance(value.get("result"), dict):
        envelope = value["result"]
        result["result"] = fields(envelope)
        rows = envelope.get("cmsArticleWebOld")
        if isinstance(rows, list):
            result["articles"] = {"count": min(len(rows), COUNT_LIMIT),
                                  "rows": [fields(row) for row in rows[:10]], "truncated": len(rows) > 10}
    return result


def _source_bindings():
    modules = {"adapter": akshare_data, "parser": eastmoney_news,
               "route": interface, "acquisition": acquisition, "diagnostics": run_diagnostics}
    return {"tool": hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
            "cli": hashlib.sha256((Path(__file__).resolve().parents[1]
                                   / "scripts/diagnose_public_news.py").read_bytes()).hexdigest(), **{
        name: hashlib.sha256(Path(module.__file__).read_bytes()).hexdigest()
        for name, module in modules.items()
    }}


def _frame_digest(frame):
    representation = {"dtypes": [str(dtype) for dtype in frame.dtypes],
                      "frame": json.loads(frame.to_json(orient="split", date_format="iso", force_ascii=True))}
    return hashlib.sha256(json.dumps(representation, sort_keys=True).encode()).hexdigest()


def _observation(start, end, *, retain, ticker=None, transport=None):
    return {
        "contract": CONTRACT, "qualification_authority": False,
        "stage": "request", "mode": "capture" if retain else "replay",
        "ticker": ticker, "transport": transport, "window": {"start": start, "end": end}, "request_count": 0,
        "body_saved": False, "projection_saved": False, "projection_replay_passed": False,
        "capture_passed": False, "acceptance_passed": False,
        "checks": {name: {"status": "not_started"} for name in (
            "network", "http", "decode", "retention", "parser", "adapter", "production", "replay",
        )},
        "selection": {"status": "not_observed", "excluded_counts": None,
                      "accepted_count": None, "coverage": "unknown"},
        "diagnostics": [], "data_gaps": ["news_window_coverage_unknown"],
        "response_representation": "unchanged response.content bytes; not compressed wire bytes",
        "historical_availability": "not_established_by_publication_date",
    }


def _check(observation, name, status, reason, *, stage=None, error=None):
    if observation["checks"].get(name, {}).get("status") == "failed":
        return
    check = {"status": status, "reason": reason}
    if stage is not None:
        check["stage"] = stage
    if error is not None:
        kind = type(error).__name__
        check["exception_type"] = kind if kind in _EXCEPTION_TYPES else "OtherError"
    observation["checks"][name] = check


def source_capture_passed(source):
    if not isinstance(source, dict) or source.get("contract") != CONTRACT or source.get("body_saved") is not True:
        return False
    if type(source.get("ticker")) is not str or source["ticker"] not in TICKERS:
        return False
    if type(source.get("transport")) is not str or source["transport"] != "direct":
        return False
    checks = source.get("checks")
    return isinstance(checks, dict) and all(
        isinstance(checks.get(name), dict) and checks[name].get("status") == "passed"
        for name in ("network", "http", "decode", "retention", "parser", "adapter", "production", "receipt")
    )


def _finish(observation):
    checks = observation["checks"]
    failure = next((check for check in checks.values() if check["status"] == "failed"), None)
    observation["stage"] = (failure.get("stage", failure["reason"]) if failure else
                            "available" if checks["production"]["status"] == "passed" else "incomplete")
    observation["capture_passed"] = observation["mode"] == "capture" and source_capture_passed(observation)
    if observation["mode"] == "live_metadata_only":
        observation["metadata_passed"] = failure is None and all(
            checks[name]["status"] == "passed" for name in
            ("network", "http", "decode", "retention", "parser", "adapter", "production"))
    return observation


def _observe_selection(observation, counts, accepted):
    if set(counts) != set(EXCLUSIONS) or any(type(counts[k]) is not int or counts[k] < 0 for k in EXCLUSIONS):
        return
    observation["selection"] = {
        "status": "observed", "excluded_counts": {k: min(counts[k], COUNT_LIMIT) for k in EXCLUSIONS},
        "accepted_count": min(accepted, COUNT_LIMIT), "coverage": "unknown",
        "counts_saturated": accepted > COUNT_LIMIT or any(counts[k] > COUNT_LIMIT for k in EXCLUSIONS),
    }


def _request_symbol(ticker, start, end, transport):
    if type(transport) is not str or transport != "direct":
        raise eastmoney_news.NewsResponseError("request_invalid")
    if type(ticker) is not str or ticker not in TICKERS:
        raise eastmoney_news.NewsResponseError("request_invalid")
    symbol, _, _ = eastmoney_news._request(ticker, start, end, ())
    return symbol


def _run(root, *, get, start, end, retain, ticker, transport, metadata_only=False,
         public_metadata_binding=None):
    if type(metadata_only) is not bool or (metadata_only and not retain):
        raise ValueError("invalid_metadata_capture_mode")
    if public_metadata_binding is not None and (not metadata_only or not callable(public_metadata_binding)):
        raise ValueError("invalid_public_metadata_binding")
    root = Path(root)
    try:
        symbol = _request_symbol(ticker, start, end, transport)
    except eastmoney_news.NewsResponseError:
        observation = _observation(None, None, retain=retain)
        if metadata_only:
            observation.update(contract=METADATA_CONTRACT, mode="live_metadata_only", byte_replay_available=False)
        _check(observation, "parser", "failed", "request_invalid", stage="request")
        return _finish(observation)
    observation = _observation(start, end, retain=retain, ticker=ticker, transport=transport)
    if metadata_only:
        observation.update(contract=METADATA_CONTRACT, mode="live_metadata_only", byte_replay_available=False)
    observation["source_bindings"] = _source_bindings()
    _check(observation, "replay", "not_requested", "body_not_retained_no_byte_replay" if metadata_only
           else "separate_offline_replay_required")
    runtime = copy.deepcopy(DEFAULT_CONFIG)
    runtime.update(tool_vendors={"get_news": "akshare"},
                   market_history_database_path=str(root / "coordinator.sqlite3"),
                   market_history_payload_root=str(root / "payloads"),
                   market_history_backup_root=str(root / "backups"))

    def observe_body(response, callback):
        try:
            content = response.content
        except Exception as error:
            _check(observation, "decode", "failed", "body_read_exception", stage="body_read", error=error)
            return
        if not isinstance(content, bytes) or len(content) > LIMIT:
            _check(observation, "decode", "failed", "response_limit_or_type", stage="decode")
            return
        observation["response_bytes"] = len(content)
        if not metadata_only:
            observation["response_sha256"] = hashlib.sha256(content).hexdigest()
        reason, rows, value, matches = _public_payload(content, callback)
        if metadata_only:
            # Optional syntax observation cannot turn rejection into success or
            # leak an observer's exception text. Cancellation still propagates.
            observation["closed_shape"] = {"contract": "news-closed-shape-v1", "classification": "unavailable",
                "wrapper": "invalid_or_unknown", "qualification_authority": False, "scopes": {}}
            with suppress(Exception):
                observation["closed_shape"] = _metadata_shape(content, value, matches)
            if public_metadata_binding is not None:
                observation["public_structure"] = _unavailable_public_structure()
                with suppress(Exception):
                    observation["public_structure"] = _public_structure_metadata(
                        content, value, matches, public_metadata_binding() is True)
        if not metadata_only:
            observation["structure"] = _safe_structure(value)
        elif value is not None:
            # Only the existing closed collection/type/count projection; no
            # arbitrary names, values or hashes of rejected/unknown content.
            with suppress(Exception):
                run_diagnostics.record_news_response_shape(value, callback_matches=matches, stage="decode")
        observation["callback_matches"] = matches
        observation["raw_count"] = min(len(rows), COUNT_LIMIT) if rows is not None else None
        observation["raw_count_saturated"] = len(rows) > COUNT_LIMIT if rows is not None else None
        if value is None:
            _check(observation, "decode", "failed", reason, stage="decode")
            return
        _check(observation, "decode", "passed", "production_decoder_accepted")
        if reason is not None:
            _check(observation, "retention", "failed", reason, stage="retention")
        elif metadata_only:
            _check(observation, "retention", "passed", "public_bytes_validated_not_saved")
        elif retain:
            try:
                with (root / BODY_NAME).open("xb") as handle:
                    handle.write(content)
            except OSError as error:
                _check(observation, "retention", "failed", "body_write_failed", stage="retention", error=error)
            else:
                observation.update(body_saved=True, saved_body_sha256=observation["response_sha256"])
                _check(observation, "retention", "passed", "exact_public_bytes_saved")
        else:
            _check(observation, "retention", "passed", "retained_public_bytes_validated")

    def bounded_get(url, *, params, headers, allow_redirects, timeout):
        try:
            inner = json.loads(params["param"])
            pagination = inner["param"]["cmsArticleWebOld"]
            valid = (url == URL and inner["keyword"] == symbol
                     and pagination["pageIndex"] == 1 and pagination["pageSize"] == 10
                     and allow_redirects is False and timeout == 15 and observation["request_count"] == 0)
        except (ValueError, KeyError, TypeError):
            valid = False
        if not valid:
            _check(observation, "network", "failed", "request_scope_or_budget", stage="request")
            raise ValueError("news request scope or budget rejected")
        observation["request_fingerprint"] = hashlib.sha256(json.dumps({
            "url": url, "params": params, "headers": headers,
            "allow_redirects": allow_redirects, "timeout": timeout,
        }, sort_keys=True).encode()).hexdigest()
        observation["request_count"] += 1
        try:
            response = get(url, params=params, headers=headers, allow_redirects=allow_redirects, timeout=timeout)
        except Exception as error:
            _check(observation, "network", "failed", "transport_exception", stage="transport", error=error)
            try:
                detail = _transport_error_detail(error)
            except Exception:
                # Optional metadata must never replace the original failure or
                # turn it into success, a retry, or a differently scoped request.
                detail = {"contract": TRANSPORT_ERROR_CONTRACT, "chain": [], "termination": "unavailable"}
            observation["checks"]["network"]["transport_detail"] = detail
            raise
        _check(observation, "network", "passed", "response_received" if retain else "offline_fixture")
        status = response.status_code
        observation["http_status"] = status if type(status) is int and 100 <= status <= 599 else None
        if status != 200:
            _check(observation, "http", "failed", "http_failure_or_redirect", stage="http")
        else:
            _check(observation, "http", "passed", "http_200")
            try:
                observe_body(response, params["cb"])
            except BaseException as failure:
                # Ownership has not reached the adapter. Close once, retaining
                # this first terminal error (including cancellation) even if
                # cleanup/its recording fails. Never retry or swallow it.
                try:
                    response.close()
                except BaseException as error:
                    with suppress(BaseException):
                        run_diagnostics.record_news_exception(
                            error, stage="response_close", role="cleanup", status=status)
                raise failure from None
            if any(observation["checks"][name]["status"] == "failed"
                   for name in ("decode", "retention")):
                # A rejected response must not reach production parsing/admission.
                # The adapter has not received ownership, so close it here.
                failed = next(observation["checks"][name] for name in ("decode", "retention")
                              if observation["checks"][name]["status"] == "failed")
                failure = run_diagnostics.NewsResponseRejected(
                    stage=failed["stage"], error_type=failed.get("exception_type", "ValueError"),
                    http_status=status,
                )
                diagnostic_failure = None
                try:
                    run_diagnostics.record_news_exception(failure, stage=failure.stage, status=status)
                except BaseException as error:
                    diagnostic_failure = error
                try:
                    response.close()
                except BaseException as error:
                    if isinstance(error, run_diagnostics.DiagnosticPersistenceError) and diagnostic_failure is None:
                        diagnostic_failure = error
                    try:
                        run_diagnostics.record_news_exception(
                            error, stage="response_close", role="cleanup", status=status)
                    except BaseException as persistence_error:
                        if diagnostic_failure is None:
                            diagnostic_failure = persistence_error
                if diagnostic_failure is not None:
                    # A diagnostic write failure remains terminal, never retryable.
                    raise diagnostic_failure from None
                raise failure from None
        # Identical response reaches production, including negative cases.
        # Production owns close(), including HTTP and parser failures.
        return response

    production_parse = eastmoney_news.parse_news_page

    def observe_parse(*args, **kwargs):
        try:
            page = production_parse(*args, **kwargs)
        except eastmoney_news.NewsResponseError as error:
            reason = error.reason if error.reason in PARSE_REASONS else "other_parser_failure"
            _check(observation, "parser", "failed", reason, stage="response_parse")
            if reason == "no_usable_articles":
                _observe_selection(observation, error.excluded_counts, 0)
            raise
        _observe_selection(observation, page.excluded_counts, len(page.rows))
        _check(observation, "parser", "passed", "associated_in_window_articles")
        return page

    production_adapter = akshare_data._stock_news_em

    def observe_adapter(symbol, start_date, end_date, *, instrument_aliases=()):
        try:
            frame = production_adapter(symbol, start_date, end_date, instrument_aliases=instrument_aliases)
        except Exception as error:
            _check(observation, "adapter", "failed", "adapter_exception", stage="adapter", error=error)
            raise
        _check(observation, "adapter", "passed", "dataframe_returned")
        if not metadata_only:
            try:
                observation["adapter_frame_sha256"] = _frame_digest(frame)
            except (ValueError, TypeError, OverflowError, UnicodeError):
                observation["adapter_frame_sha256"] = None
        return frame

    def observe_diagnostic(event):
        allowed = {"news_business_status", "news_exception"}
        if metadata_only:
            allowed.add("news_response_shape")
        if event["kind"] in allowed and len(observation["diagnostics"]) < 8:
            # Existing emitters contain only safe enums/types, no body/addresses.
            observation["diagnostics"].append(event)

    with (
        patch.object(akshare_data, "requests", SimpleNamespace(get=bounded_get)),
        patch.object(eastmoney_news, "parse_news_page", observe_parse),
        patch.object(akshare_data, "_stock_news_em", observe_adapter),
        patch.object(config, "_config", runtime),
        run_diagnostics.diagnostic_scope(observe_diagnostic),
        market_snapshot.authoritative_snapshot_run(),
    ):
        try:
            acquired = interface.route_to_vendor_acquired(
                "get_news", symbol + ".SS", start, end, tool_call_id="news-diagnostic",
                source_ref="diagnostic:public-news", capability="get_news",
            )
        except Exception as error:
            _check(observation, "production", "failed", "route_exception", stage="admission", error=error)
            return _finish(observation)
    if acquired.artifact is None:
        _check(observation, "production", "failed", "artifact_unavailable", stage="admission")
    else:
        _check(observation, "production", "passed", "artifact_available")
        if not metadata_only:
            observation["artifact_sha256"] = hashlib.sha256(acquired.artifact.raw_text.encode()).hexdigest()
    return _finish(observation)


def capture(root, *, get, start, end, ticker, transport, metadata_only=False):
    """Live capture; explicit metadata-only mode never retains replayable bytes."""
    result = _run(root, get=get, start=start, end=end, retain=True, ticker=ticker,
                  transport=transport, metadata_only=metadata_only)
    return _save_capture_receipt(result, root)


def _save_capture_receipt(result, root):
    """Publish once after the caller has completed its owned cleanup."""
    _check(result, "receipt", "passed", "capture_receipt_saved")
    _finish(result)
    try:
        with (Path(root) / "capture.json").open("x", encoding="utf-8") as handle:
            json.dump(result, handle, indent=2)
    except OSError as error:
        _check(result, "receipt", "failed", "capture_receipt_write_failed", stage="receipt", error=error)
        _finish(result)
    return result


def _read_retained(root, name):
    path = root / name
    for item in (root, path):
        info = item.lstat()
        if stat.S_ISLNK(info.st_mode) or getattr(info, "st_file_attributes", 0) & 1024:
            raise ValueError("invalid replay file")
    before = path.stat()
    if not stat.S_ISREG(before.st_mode) or before.st_nlink != 1 or before.st_size > LIMIT:
        raise ValueError("invalid replay file")
    data = path.read_bytes()
    after = path.stat()
    if len(data) > LIMIT or (before.st_ino, before.st_size, before.st_mtime_ns) != (
            after.st_ino, after.st_size, after.st_mtime_ns):
        raise ValueError("changed replay file")
    return data


def replay(root, *, start, end, ticker, transport):
    root = Path(root)
    try:
        _request_symbol(ticker, start, end, transport)
    except eastmoney_news.NewsResponseError:
        result = _observation(None, None, retain=False)
        _check(result, "replay", "failed", "request_invalid", stage="request")
        return _finish(result)
    result = _observation(start, end, retain=False, ticker=ticker, transport=transport)
    result["replay_kind"] = "exact_bytes"
    try:
        captured = json.loads(_read_retained(root, "capture.json"))
        if not isinstance(captured, dict) or captured.get("contract") != CONTRACT:
            _check(result, "replay", "failed", "unsupported_capture_contract", stage="replay")
            return _finish(result)
        if captured.get("ticker") != ticker:
            _check(result, "replay", "failed", "ticker_mismatch", stage="replay")
            return _finish(result)
        if captured.get("transport") != transport:
            _check(result, "replay", "failed", "transport_mismatch", stage="replay")
            return _finish(result)
        if captured.get("body_saved") is not True:
            raise ValueError("capture has no retained original")
        body = _read_retained(root, BODY_NAME)
        if hashlib.sha256(body).hexdigest() != captured.get("saved_body_sha256"):
            _check(result, "replay", "failed", "retained_body_digest_mismatch", stage="replay")
            return _finish(result)
    except (OSError, ValueError, KeyError) as error:
        _check(result, "replay", "failed", "replay_input_error", stage="replay", error=error)
        return _finish(result)
    result = _run(root, get=lambda *a, **k: SimpleNamespace(
        status_code=200, content=body, close=lambda: None,
    ), start=start, end=end, retain=False, ticker=ticker, transport=transport)
    result["replay_kind"] = "exact_bytes"
    bindings = ("ticker", "transport", "source_bindings", "request_fingerprint", "window", "artifact_sha256", "adapter_frame_sha256")
    for name in bindings:
        result["same_" + name] = bool(captured.get(name)) and result.get(name) == captured.get(name)
    result["source_capture_passed"] = captured.get("capture_passed") is True and source_capture_passed(captured)
    successful = all(result["checks"][name]["status"] == "passed"
                     for name in ("network", "http", "decode", "retention", "parser", "adapter", "production"))
    mismatch = next((name for name in bindings if not result["same_" + name]), None)
    reason = (mismatch + "_mismatch" if mismatch else
              "source_capture_failed" if not result["source_capture_passed"] else
              "production_replayed" if successful else "production_replay_failed")
    passed = successful and mismatch is None and result["source_capture_passed"]
    _check(result, "replay", "passed" if passed else "failed", reason, stage="replay")
    result["acceptance_passed"] = passed
    return _finish(result)

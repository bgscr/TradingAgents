"""One-response diagnostic through the installed SDK and production news route.

The caller must authorize any supplied live GET. No default live transport is
selected here. Only recognized public-news JSONP is retained for exact text replay.
"""

from __future__ import annotations

import copy
import hashlib
import inspect
import json
import re
from pathlib import Path
from types import FunctionType, SimpleNamespace
from unittest.mock import patch

from tradingagents.dataflows import akshare_data, config, interface, market_snapshot
from tradingagents.default_config import DEFAULT_CONFIG

URL = "https://search-api-web.eastmoney.com/search/jsonp"
LIMIT = 1024 * 1024
PUBLIC_FIELDS = {"date", "mediaName", "code", "title", "content", "image"}
PUBLIC_COUNTS = {"code", "hits", "total", "count"}
# This reviewed SDK reads only result.cmsArticleWebOld. A changed SDK must be
# reviewed before dropping envelope fields, even if a projected example parses.
PROJECTION_SDK_SHA256 = "f23960660f28d8d158587998fa307fd9b22477cac7520268f69d895d2b3e575f"
# This SDK overwrites url/关键词 and discards image. No other row column is dropped.
PROJECTION_OMITTED_FIELDS = {"url", "关键词", "image"}
STRUCTURE_LABELS = PUBLIC_FIELDS | PUBLIC_COUNTS | {
    "result", "cmsArticleWebOld", "success", "message", "msg", "version", "type", "totalCount",
}
SENSITIVE_TEXT = re.compile(
    r"(?i)(?:authorization\s*[\"']?\s*[:=]|bearer\s+|(?:api[_-]?key|access[_-]?token|"
    r"refresh[_-]?token|token|password|secret|cookie|credential)\s*[\"']?\s*[:=]|"
    r"-----BEGIN [A-Z ]*PRIVATE KEY-----)"
)


def _unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate JSON field")
        result[key] = value
    return result


def _article_problem(rows):
    if not isinstance(rows, list):
        return "articles_not_list"
    if len(rows) > 10:
        return "article_limit"
    for row in rows:
        if (
            not isinstance(row, dict)
            or not {"date", "mediaName", "code", "title", "content"} <= set(row) <= PUBLIC_FIELDS
        ):
            return "unknown_or_missing_article_fields"
        if any(not isinstance(value, str) for value in row.values()):
            return "non_string_article_field"
        if any(SENSITIVE_TEXT.search(value) for value in row.values()):
            return "sensitive_article_text"
    return None


def _project_articles(rows):
    projected = [
        {key: value for key, value in row.items() if key not in PROJECTION_OMITTED_FIELDS}
        if isinstance(row, dict) else row
        for row in rows
    ] if isinstance(rows, list) else rows
    return projected, _article_problem(projected)


def _public_payload(text, callback):
    if not text.startswith(callback + "(") or not text.endswith(")"):
        return "jsonp", "invalid_jsonp", None, None
    try:
        # Match the SDK's strip semantics, but reject duplicate keys for retention.
        value = json.loads(text.strip(callback + "(")[:-1], object_pairs_hook=_unique_object)
    except (ValueError, TypeError, RecursionError):
        return "json", "invalid_json_or_duplicate_fields", None, None
    if not isinstance(value, dict) or not isinstance(value.get("result"), dict):
        return "schema", "invalid_envelope", None, value
    result = value["result"]
    rows = result.get("cmsArticleWebOld")
    if not isinstance(rows, list):
        return "schema", "articles_not_list", None, value
    # These restrictions apply to exact original text, not the separate projection.
    if set(value) - {"result", "success"} - PUBLIC_COUNTS:
        return "schema", "unknown_envelope_fields", rows, value
    if set(result) - {"cmsArticleWebOld"} - PUBLIC_COUNTS:
        return "schema", "unknown_result_fields", rows, value
    for envelope in (value, result):
        if any(
            type(envelope[key]) is not int or not 0 <= envelope[key] <= 2**53
            for key in PUBLIC_COUNTS & envelope.keys()
        ):
            return "schema", "invalid_public_metadata", rows, value
    if "success" in value and value["success"] is not True:
        return "schema", "unsuccessful_envelope", rows, value
    problem = _article_problem(rows)
    return "schema" if problem else "sdk_dataframe", problem, rows, value


def _safe_structure(value):
    """Bounded types/counts only; unknown names are hashed, values never copied."""
    def kind(item):
        return {
            dict: "object", list: "array", str: "string", bool: "boolean",
            int: "number", float: "number", type(None): "null",
        }[type(item)]

    def fields(obj):
        if not isinstance(obj, dict):
            return {"type": kind(obj)}
        items = []
        for name, item in list(obj.items())[:32]:
            entry = {"type": kind(item)}
            if name in STRUCTURE_LABELS:
                entry["name"] = name
            else:
                entry["name_sha256"] = hashlib.sha256(name.encode("utf-8", errors="surrogatepass")).hexdigest()
                entry["name_length"] = len(name)
            if isinstance(item, (dict, list, str)):
                entry["size"] = len(item)
            items.append(entry)
        return {"type": "object", "field_count": len(obj), "fields": items, "truncated": len(obj) > 32}

    result = {"envelope": fields(value)}
    if isinstance(value, dict) and isinstance(value.get("result"), dict):
        envelope = value["result"]
        result["result"] = fields(envelope)
        rows = envelope.get("cmsArticleWebOld")
        if isinstance(rows, list):
            result["articles"] = {"count": len(rows), "rows": [fields(row) for row in rows[:10]], "truncated": len(rows) > 10}
    return result


def _sdk_frame_digest(frame):
    # Include all SDK rows, column/index order and dtypes, not only the rendered
    # in-window artifact. Hash only; never persist SDK content in a receipt.
    representation = {
        "dtypes": [str(dtype) for dtype in frame.dtypes],
        "frame": json.loads(frame.to_json(orient="split", date_format="iso", force_ascii=True)),
    }
    return hashlib.sha256(json.dumps(representation, sort_keys=True).encode()).hexdigest()


def _summary(values=None):
    result = {"count": None, "invalid_dates": None, "date_min": None, "date_max": None}
    if values is None:
        return result
    result["count"] = len(values)
    try:
        dates = akshare_data.pd.to_datetime(
            akshare_data.pd.Series(values, dtype=object), errors="coerce"
        )
        result["invalid_dates"] = int(dates.isna().sum())
        valid = dates.dropna()
        if not valid.empty:
            result.update(date_min=valid.min().isoformat(), date_max=valid.max().isoformat())
    except (ValueError, TypeError, AttributeError, OverflowError):
        # Observation failure must not change production parsing or expose a value.
        result["reason"] = "date_summary_unavailable"
    return result


def _observation(start, end, *, retain):
    return {
        "stage": "request",
        "mode": "capture" if retain else "replay",
        "window": {"start": start, "end": end},
        "request_count": 0,
        "body_saved": False,
        "projection_saved": False,
        "projection_replay_passed": False,
        "capture_passed": False,
        "acceptance_passed": False,
        "checks": {
            name: {"status": "not_started"}
            for name in ("network", "http", "decode", "retention", "sdk", "production", "replay")
        },
        "articles": {name: _summary() for name in ("raw", "sdk", "filtered")},
        "response_representation": "SDK decoded text; content bytes are decompressed, not wire bytes",
    }


def _check(observation, name, status, reason, *, stage=None, error=None):
    # A downstream outcome (or rejected extra attempt) cannot erase the cause.
    if observation["checks"].get(name, {}).get("status") == "failed":
        return
    check = {"status": status, "reason": reason}
    if stage is not None:
        check["stage"] = stage
    if error is not None:
        check["exception_type"] = type(error).__name__
    observation["checks"][name] = check


def _finish(observation):
    checks = observation["checks"]
    failure = next((c for c in checks.values() if c["status"] == "failed"), None)
    observation["stage"] = (
        failure.get("stage", failure["reason"]) if failure else
        "available" if checks["production"]["status"] == "passed" else "incomplete"
    )
    observation["capture_passed"] = observation["mode"] == "capture" and all(
        check["status"] == "passed" for name, check in checks.items() if name not in {"replay", "projection"}
    )
    return observation


def _run(root, *, get, start, end, retain):
    root = Path(root)
    observation = _observation(start, end, retain=retain)
    _check(observation, "replay", "not_requested", "separate_offline_replay_required")
    _check(observation, "projection", "not_requested", "exact_text_preferred")
    sdk = akshare_data.ak.stock_news_em
    observation["sdk_sha256"] = hashlib.sha256(inspect.getsource(sdk).encode()).hexdigest()
    runtime = copy.deepcopy(DEFAULT_CONFIG)
    runtime.update(
        tool_vendors={"get_news": "akshare"},
        market_history_database_path=str(root / "coordinator.sqlite3"),
        market_history_payload_root=str(root / "payloads"),
        market_history_backup_root=str(root / "backups"),
    )

    def bounded_get(url, *, params, headers):
        inner = json.loads(params["param"])
        pagination = inner["param"]["cmsArticleWebOld"]
        if (
            url != URL
            or inner["keyword"] != "600895"
            or pagination["pageIndex"] != 1
            or pagination["pageSize"] != 10
            or observation["request_count"]
        ):
            _check(observation, "network", "failed", "request_scope_or_budget", stage="request")
            raise RuntimeError("news request scope or budget rejected")
        observation["request_fingerprint"] = hashlib.sha256(
            json.dumps({"url": url, "params": params, "headers": headers}, sort_keys=True).encode()
        ).hexdigest()
        observation["request_count"] += 1
        try:
            response = get(url, params=params, headers=headers, allow_redirects=False, timeout=20)
        except Exception as exc:
            _check(observation, "network", "failed", "transport_exception", stage="transport", error=exc)
            raise
        _check(observation, "network", "passed", "response_received" if retain else "offline_fixture")
        observation["http_status"] = response.status_code
        if response.status_code != 200:
            _check(observation, "http", "failed", "http_failure_or_redirect", stage="http")
            raise ValueError("HTTP failure or redirect stopped")
        _check(observation, "http", "passed", "http_200")
        # Do not put arbitrary header values or exception messages in receipts.
        content_type = str(response.headers.get("Content-Type", "")).split(";")[0].lower()
        encoding = str(response.headers.get("Content-Encoding", "")).lower()
        observation["content_type"] = content_type if content_type in {
            "", "application/javascript", "text/javascript", "application/json", "text/plain"
        } else "unrecognized"
        observation["content_encoding"] = encoding if encoding in {
            "", "gzip", "deflate", "br", "identity"
        } else "unrecognized"
        try:
            content, text = response.content, response.text
            encoded_text = text.encode("utf-8")
        except Exception as exc:
            _check(observation, "decode", "failed", "decode_exception", stage="decode", error=exc)
            raise
        observation.update(
            response_bytes=len(content), response_sha256=hashlib.sha256(content).hexdigest()
        )
        if len(content) > LIMIT or len(encoded_text) > LIMIT:
            _check(observation, "decode", "failed", "response_limit", stage="response_limit")
            raise ValueError("response too large")
        _check(observation, "decode", "passed", "within_response_limit")
        stage, reason, rows, value = _public_payload(text, params["cb"])
        observation["structure"] = _safe_structure(value)
        observation["jsonp"] = {
            "expected_prefix": text.startswith(params["cb"] + "("),
            "expected_suffix": text.endswith(")"),
        }
        if rows is not None:
            observation["articles"]["raw"] = _summary([
                row.get("date") if isinstance(row, dict) else None for row in rows
            ])
        public = reason is None
        observation["public_schema"] = public
        if not public:
            _check(observation, "retention", "failed", reason, stage=stage)
        elif retain:
            try:
                with (root / "public-response.txt").open("x", encoding="utf-8", newline="") as handle:
                    handle.write(text)
            except OSError as exc:
                _check(observation, "retention", "failed", "body_write_failed", stage="retention", error=exc)
            else:
                observation["body_saved"] = True
                observation["saved_text_sha256"] = hashlib.sha256(encoded_text).hexdigest()
                _check(observation, "retention", "passed", "exact_public_text_saved")
        else:
            _check(observation, "retention", "passed", "retained_public_text_validated")
        if retain and reason is not None and rows is not None:
            # Metadata is outside this SDK's input contract. Within rows, omit
            # only proven overwritten/discarded inputs, then validate the core.
            projected, problem = _project_articles(rows)
            if observation["sdk_sha256"] != PROJECTION_SDK_SHA256:
                problem = "sdk_projection_not_reviewed"
            if problem:
                _check(observation, "projection", "failed", problem, stage="projection")
            else:
                projection = params["cb"] + "(" + json.dumps(
                    {"result": {"cmsArticleWebOld": projected}}, ensure_ascii=True,
                ) + ")"
                if len(projection.encode()) > LIMIT:
                    _check(observation, "projection", "failed", "projection_size_limit", stage="projection")
                    return response
                try:
                    with (root / "public-projection.txt").open("x", encoding="utf-8", newline="") as handle:
                        handle.write(projection)
                except OSError as exc:
                    _check(observation, "projection", "failed", "projection_write_failed", stage="projection", error=exc)
                else:
                    observation["projection_saved"] = True
                    observation["projection_sha256"] = hashlib.sha256(projection.encode()).hexdigest()
                    _check(observation, "projection", "passed", "validated_projection_saved_not_original")
        # Feed the original response to the actual SDK, including negative cases.
        return response

    globals_copy = {**sdk.__globals__, "requests": SimpleNamespace(get=bounded_get)}
    clone = FunctionType(
        sdk.__code__, globals_copy, sdk.__name__, sdk.__defaults__, sdk.__closure__
    )
    clone.__kwdefaults__ = sdk.__kwdefaults__
    production_sdk_call = akshare_data._stock_news_em

    def observe_sdk(symbol):
        try:
            result = production_sdk_call(symbol)
        except Exception as exc:
            observation["exception_type"] = type(exc).__name__
            _check(observation, "sdk", "failed", "sdk_exception", stage="sdk_dataframe", error=exc)
            raise
        _check(observation, "sdk", "passed", "dataframe_returned")
        observation["articles"]["sdk"] = _summary(result["发布时间"].tolist())
        try:
            observation["sdk_frame_sha256"] = _sdk_frame_digest(result)
        except (ValueError, TypeError, OverflowError, UnicodeError):
            observation["sdk_frame_sha256"] = None
            observation["sdk_frame_digest_reason"] = "frame_digest_unavailable"
        return result

    production_window = akshare_data._news_in_window

    def observe_window(raw, start_date, end_date):
        try:
            result = production_window(raw, start_date, end_date)
        except Exception as exc:
            _check(observation, "production", "failed", "date_window_exception", stage="window_or_admission", error=exc)
            raise
        observation["articles"]["filtered"] = _summary(result["发布时间"].tolist())
        return result

    with (
        patch.object(akshare_data.ak, "stock_news_em", clone),
        patch.object(akshare_data, "_stock_news_em", observe_sdk),
        patch.object(akshare_data, "_news_in_window", observe_window),
        patch.object(config, "_config", runtime),
        market_snapshot.authoritative_snapshot_run(),
    ):
        try:
            acquired = interface.route_to_vendor_acquired(
                "get_news",
                "600895.SS",
                start,
                end,
                tool_call_id="news-diagnostic",
                source_ref="diagnostic:public-news",
                capability="get_news",
            )
        except Exception as exc:
            _check(observation, "production", "failed", "route_exception", stage="window_or_admission", error=exc)
            return _finish(observation)
    observation["outcomes"] = [getattr(item, "reason", None) for item in acquired.outcomes]
    if acquired.artifact is not None:
        _check(observation, "production", "passed", "artifact_available")
        observation["artifact_sha256"] = hashlib.sha256(
            acquired.artifact.raw_text.encode()
        ).hexdigest()
    else:
        _check(observation, "production", "failed", "artifact_unavailable", stage="window_or_admission")
    return _finish(observation)


def capture(root, *, get, start, end):
    result = _run(root, get=get, start=start, end=end, retain=True)
    _check(result, "receipt", "passed", "capture_receipt_saved")
    _finish(result)
    try:
        with (Path(root) / "capture.json").open("x", encoding="utf-8") as handle:
            json.dump(result, handle, indent=2)
    except OSError as exc:
        _check(result, "receipt", "failed", "capture_receipt_write_failed", stage="receipt", error=exc)
        _finish(result)
    return result


def replay(root, *, start, end):
    root = Path(root)
    result = _observation(start, end, retain=False)
    result["replay"] = True

    def valid_file(name):
        path = root / name
        return not path.is_symlink() and path.is_file() and path.stat().st_size <= LIMIT

    try:
        if not valid_file("capture.json"):
            _check(result, "replay", "failed", "invalid_replay_file", stage="replay")
            return _finish(result)
        captured = json.loads((root / "capture.json").read_text(encoding="utf-8"))
        if not isinstance(captured, dict):
            raise ValueError("invalid capture receipt")
        if captured.get("body_saved") is True:
            kind, name, digest_key = "exact_text", "public-response.txt", "saved_text_sha256"
        elif captured.get("projection_saved") is True:
            kind, name, digest_key = "validated_projection", "public-projection.txt", "projection_sha256"
        else:
            raise ValueError("capture receipt has no retained body")
        if not valid_file(name):
            _check(result, "replay", "failed", "invalid_replay_file", stage="replay")
            return _finish(result)
        text = (root / name).read_bytes().decode("utf-8")
        result["replay_kind"] = kind
        if hashlib.sha256(text.encode()).hexdigest() != captured[digest_key]:
            _check(result, "replay", "failed", "retained_text_digest_mismatch", stage="replay")
            return _finish(result)
    except (OSError, ValueError, KeyError) as exc:
        _check(result, "replay", "failed", "replay_input_error", stage="replay", error=exc)
        return _finish(result)
    result = _run(
        root,
        get=lambda *a, **k: SimpleNamespace(
            status_code=200, content=text.encode(), text=text, headers={}
        ),
        start=start,
        end=end,
        retain=False,
    )
    result["replay"] = True
    result["replay_kind"] = kind
    result["same_sdk"] = result["sdk_sha256"] == captured.get("sdk_sha256")
    result["same_request"] = result.get("request_fingerprint") == captured.get("request_fingerprint")
    result["same_window"] = result["window"] == captured.get("window")
    result["same_artifact"] = bool(result.get("artifact_sha256")) and result.get("artifact_sha256") == captured.get("artifact_sha256")
    result["same_sdk_frame"] = bool(captured.get("sdk_frame_sha256")) and result.get("sdk_frame_sha256") == captured.get("sdk_frame_sha256")
    checks = result["checks"]
    passed = all(checks[name]["status"] == "passed" for name in ("network", "http", "decode", "retention", "sdk", "production"))
    bindings = ("sdk", "request", "window", "artifact")
    if kind == "validated_projection" or captured.get("sdk_frame_sha256"):
        bindings += ("sdk_frame",)
    for name in bindings:
        if not result["same_" + name]:
            _check(result, "replay", "failed", name + "_mismatch", stage="replay")
            break
    else:
        _check(result, "replay", "passed" if passed else "failed", "production_replayed" if passed else "production_replay_failed", stage="replay")
    if kind == "validated_projection":
        source_checks = captured.get("checks", {})
        if result["sdk_sha256"] != PROJECTION_SDK_SHA256:
            _check(result, "replay", "failed", "sdk_projection_not_reviewed", stage="replay")
        elif not isinstance(source_checks, dict) or not all(
            isinstance(source_checks.get(name), dict) and source_checks[name].get("status") == "passed"
            for name in ("sdk", "production")
        ):
            _check(result, "replay", "failed", "source_production_unverified", stage="replay")
    result["source_capture_passed"] = captured.get("capture_passed") is True
    replay_passed = passed and checks["replay"]["status"] == "passed"
    result["projection_replay_passed"] = (
        kind == "validated_projection" and replay_passed
    )
    result["acceptance_passed"] = kind == "exact_text" and result["source_capture_passed"] and replay_passed
    return _finish(result)

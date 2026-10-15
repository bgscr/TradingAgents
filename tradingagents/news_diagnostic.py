"""One-response diagnostic through the installed SDK and production news route.

The caller must authorize any supplied live GET. No default live transport is
selected here. Only recognized public-news JSONP is retained for exact text replay.
"""

from __future__ import annotations

import copy
import hashlib
import inspect
import json
from pathlib import Path
from types import FunctionType, SimpleNamespace
from unittest.mock import patch

from tradingagents.dataflows import akshare_data, config, interface, market_snapshot
from tradingagents.default_config import DEFAULT_CONFIG

URL = "https://search-api-web.eastmoney.com/search/jsonp"
LIMIT = 1024 * 1024
PUBLIC_FIELDS = {"date", "mediaName", "code", "title", "content", "image"}


def _public_payload(text, callback):
    if not text.startswith(callback + "(") or not text.endswith(")"):
        return "jsonp", False
    try:
        # Deliberately identical to the installed SDK's parsing semantics.
        value = json.loads(text.strip(callback + "(")[:-1])
    except (ValueError, TypeError):
        return "json", False
    if not isinstance(value, dict) or set(value) != {"result"}:
        return "schema", False
    result = value["result"]
    if not isinstance(result, dict) or set(result) != {"cmsArticleWebOld"}:
        return "schema", False
    rows = result["cmsArticleWebOld"]
    if not isinstance(rows, list) or len(rows) > 10:
        return "schema", False
    for row in rows:
        if (
            not isinstance(row, dict)
            or not {"date", "mediaName", "code", "title", "content"} <= set(row) <= PUBLIC_FIELDS
        ):
            return "schema", False
        if any(not isinstance(value, str) for value in row.values()):
            return "schema", False
    return "sdk_dataframe", True


def _run(root, *, get, start, end, retain):
    root = Path(root)
    observation = {
        "stage": "request",
        "request_count": 0,
        "body_saved": False,
        "response_representation": "SDK decoded text; content bytes are decompressed, not wire bytes",
    }
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
            raise RuntimeError("news request scope or budget rejected")
        observation["request_fingerprint"] = hashlib.sha256(
            json.dumps({"url": url, "params": params, "headers": headers}, sort_keys=True).encode()
        ).hexdigest()
        observation["request_count"] += 1
        observation["stage"] = "transport"
        response = get(url, params=params, headers=headers, allow_redirects=False, timeout=20)
        observation["http_status"] = response.status_code
        observation["stage"] = "http"
        if response.status_code != 200:
            raise ValueError("HTTP failure or redirect stopped")
        observation["stage"] = "decode"
        observation["content_type"] = str(response.headers.get("Content-Type", ""))[:128]
        observation["content_encoding"] = str(response.headers.get("Content-Encoding", ""))[:128]
        content, text = response.content, response.text
        observation.update(
            response_bytes=len(content), response_sha256=hashlib.sha256(content).hexdigest()
        )
        if len(content) > LIMIT or len(text.encode("utf-8")) > LIMIT:
            observation["stage"] = "response_limit"
            raise ValueError("response too large")
        stage, public = _public_payload(text, params["cb"])
        observation["stage"] = stage
        observation["public_schema"] = public
        if public and retain:
            with (root / "public-response.txt").open("x", encoding="utf-8", newline="") as handle:
                handle.write(text)
            observation["body_saved"] = True
            observation["saved_text_sha256"] = hashlib.sha256(text.encode()).hexdigest()
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
            raise
        observation["stage"] = "window_or_admission"
        return result

    with (
        patch.object(akshare_data.ak, "stock_news_em", clone),
        patch.object(akshare_data, "_stock_news_em", observe_sdk),
        patch.object(config, "_config", runtime),
        market_snapshot.authoritative_snapshot_run(),
    ):
        acquired = interface.route_to_vendor_acquired(
            "get_news",
            "600895.SS",
            start,
            end,
            tool_call_id="news-diagnostic",
            source_ref="diagnostic:public-news",
            capability="get_news",
        )
    observation["outcomes"] = [getattr(item, "reason", None) for item in acquired.outcomes]
    if acquired.artifact is not None:
        observation["stage"] = "available"
        observation["artifact_sha256"] = hashlib.sha256(
            acquired.artifact.raw_text.encode()
        ).hexdigest()
    return observation


def capture(root, *, get, start, end):
    result = _run(root, get=get, start=start, end=end, retain=True)
    with (Path(root) / "capture.json").open("x", encoding="utf-8") as handle:
        json.dump(result, handle, indent=2)
    return result


def replay(root, *, start, end):
    root = Path(root)
    captured = json.loads((root / "capture.json").read_text(encoding="utf-8"))
    text = (root / "public-response.txt").read_bytes().decode("utf-8")
    if hashlib.sha256(text.encode()).hexdigest() != captured["saved_text_sha256"]:
        raise ValueError("retained text digest mismatch")
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
    result["same_sdk"] = result["sdk_sha256"] == captured["sdk_sha256"]
    result["same_request"] = result["request_fingerprint"] == captured["request_fingerprint"]
    return result

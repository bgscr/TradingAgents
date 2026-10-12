"""Per-client Tushare SDK binding to one fixed HTTPS endpoint.

The installed SDK has no transport option. Keep its query code, but give each
client a private globals mapping with our requests-compatible POST boundary.
Neither the SDK module, Requests nor the process environment is modified.
"""
from __future__ import annotations

import http.client
import importlib
import json
import math
import ssl
from dataclasses import dataclass
from types import FunctionType, MethodType

HTTPS_BASE = "https://api.waditu.com/dataapi"
ENDPOINTS = frozenset({
    "daily", "adj_factor", "suspend_d", "trade_cal",
    "stock_basic", "daily_basic", "namechange",
})
MAX_RESPONSE_BYTES = 16 * 1024 * 1024


class TushareHTTPSFailure(Exception):
    """Only fixed local codes, never upstream exception text or response bodies."""

    def __init__(self, code: str, status_code: int | None = None,
                 retry_after: float | None = None):
        self.code = code
        self.status_code = status_code
        self.retry_after = retry_after
        super().__init__(f"Tushare HTTPS request failed: {code}")


def validate_request(url, payload, timeout):
    if (type(payload) is not dict
            or set(payload) != {"api_name", "token", "params", "fields"}
            or type(payload["api_name"]) is not str
            or payload["api_name"] not in ENDPOINTS
            or url != HTTPS_BASE + "/" + payload["api_name"]
            or type(payload["token"]) is not str or not payload["token"]
            or type(payload["params"]) is not dict
            or payload["params"].get("ts_type_name") != HTTPS_BASE
            or type(payload["fields"]) is not str
            or type(timeout) not in (int, float) or not 0 < timeout <= 30):
        raise TushareHTTPSFailure("request_rejected")


def send_once(url, payload, timeout):
    """One HTTPS exchange; no redirect following, retry, proxy or netrc lookup."""
    validate_request(url, payload, timeout)
    connection = None
    failure = None
    result = None
    try:
        body = json.dumps(payload, ensure_ascii=False, allow_nan=False).encode("utf-8")
        context = ssl.create_default_context(purpose=ssl.Purpose.SERVER_AUTH)
        context.minimum_version = ssl.TLSVersion.TLSv1_2
        if not context.check_hostname or context.verify_mode != ssl.CERT_REQUIRED:
            failure = ("certificate_verification_required", None)
        else:
            connection = http.client.HTTPSConnection(
                "api.waditu.com", timeout=timeout, context=context,
            )
            connection.request(
                "POST", "/dataapi/" + payload["api_name"], body=body,
                headers={"Content-Type": "application/json", "Connection": "close"},
            )
            response = connection.getresponse()
            if response.status != 200:
                retry_after = None
                if response.status == 429:
                    try:
                        value = float(response.getheader("Retry-After", ""))
                        if math.isfinite(value) and value >= 0:
                            retry_after = value
                    except (TypeError, ValueError):
                        pass
                failure = ("rate_limited" if response.status == 429 else "http_status_rejected",
                           429 if response.status == 429 else None, retry_after)
            else:
                content = response.read(MAX_RESPONSE_BYTES + 1)
                if len(content) > MAX_RESPONSE_BYTES:
                    failure = ("response_too_large", None)
                else:
                    result = (response.status, content)
    except ssl.SSLCertVerificationError:
        failure = ("certificate_verification_failed", None)
    except Exception:
        failure = ("transport_failed", None)
    finally:
        if connection is not None:
            try:
                connection.close()
            except Exception:
                failure = ("transport_failed", None)
    # Raise outside handlers: raw failures do not survive in exception context.
    if failure is not None:
        raise TushareHTTPSFailure(*failure)
    return result


@dataclass(frozen=True)
class _Response:
    text: str


class HTTPSPost:
    """The small Requests interface used by the supported SDK query method."""

    def post(self, url, *, json, timeout=30, **kwargs):
        if kwargs:
            raise TushareHTTPSFailure("request_rejected")
        _, content = send_once(url, json, timeout)
        failure = None
        text = None
        try:
            text = content.decode("utf-8")
            decoded = __import__("json").loads(text)
            if type(decoded) is not dict or type(decoded.get("code")) is not int:
                failure = ("response_invalid", None)
            elif decoded["code"] != 0:
                message = str(decoded.get("msg", "")).casefold()
                throttled = any(part in message for part in (
                    "too many requests", "rate limit", "频次", "每分钟",
                ))
                failure = ("rate_limited", 429) if throttled else ("provider_rejected", None)
            elif (type(decoded.get("data")) is not dict
                  or type(decoded["data"].get("fields")) is not list
                  or type(decoded["data"].get("items")) is not list):
                failure = ("response_invalid", None)
        except Exception:
            failure = ("response_invalid", None)
        if failure is not None:
            raise TushareHTTPSFailure(*failure)
        return _Response(text)


class _Client:
    def __init__(self, query):
        self._sdk_query = query

    def query(self, api_name, fields="", **kwargs):
        if type(api_name) is not str or api_name not in ENDPOINTS:
            raise TushareHTTPSFailure("request_rejected")
        failure = None
        try:
            return self._sdk_query(api_name, fields=fields, **kwargs)
        except TushareHTTPSFailure as exc:
            failure = (exc.code, exc.status_code, exc.retry_after)
        except Exception:
            failure = ("sdk_query_failed", None)
        raise TushareHTTPSFailure(*failure)


def create_https_client(token):
    """Bind a client instance; unsupported SDK layouts fail before any request."""
    if type(token) is not str or not token:
        raise TushareHTTPSFailure("token_required")
    sdk = importlib.import_module("tushare")
    client = sdk.pro_api(token)
    query = type(client).query
    if (type(query) is not FunctionType or query.__closure__
            or "requests" not in query.__code__.co_names
            or "post" not in query.__code__.co_names
            or object.__getattribute__(client, "_DataApi__http_url") not in {
                "http://api.waditu.com/dataapi", HTTPS_BASE,
            }):
        raise TushareHTTPSFailure("unsupported_sdk_layout")
    client._DataApi__http_url = HTTPS_BASE
    private_query = FunctionType(
        query.__code__, {**query.__globals__, "requests": HTTPSPost()},
        query.__name__, query.__defaults__,
    )
    private_query.__kwdefaults__ = query.__kwdefaults__
    return _Client(MethodType(private_query, client))

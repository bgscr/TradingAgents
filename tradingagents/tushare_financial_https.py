"""Explicit CURRENT financial transport; no SDK default endpoint or retries."""

from __future__ import annotations

import http.client
import json
import math
import os
import re
import ssl
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime

import pandas as pd

from tradingagents.market_history.coordinator import PhysicalAttemptFailure, PhysicalAttemptOutcome

DESTINATIONS = {
    "https://api.tushare.pro": ("api.tushare.pro", "/"),
    "https://api.waditu.com/dataapi": ("api.waditu.com", "/dataapi/"),
}
ENDPOINTS = frozenset({"income", "balancesheet", "cashflow"})
MAX_RESPONSE_BYTES = 16 * 1024 * 1024


def _retry_after_seconds(value):
    """Honor both HTTP delta-seconds and HTTP-date without shortening cooldown."""
    try:
        seconds = float(value)
        if math.isfinite(seconds) and seconds >= 0:
            return seconds
    except (TypeError, ValueError):
        pass
    try:
        deadline = parsedate_to_datetime(value)
        if deadline.tzinfo is None:
            deadline = deadline.replace(tzinfo=timezone.utc)
        return max(0.0, (deadline - datetime.now(timezone.utc)).total_seconds())
    except (TypeError, ValueError, OverflowError):
        return None


def create_client(destination):
    if not isinstance(destination, str) or destination not in DESTINATIONS:
        raise ValueError("financial HTTPS destination must be explicitly configured")
    token = os.getenv("TUSHARE_TOKEN")
    if not token:
        raise PhysicalAttemptFailure(
            outcome=PhysicalAttemptOutcome.AUTHENTICATION,
            retryable=False,
            error_code="financial_authentication_required",
        )
    return _Client(destination, token)


class _Client:
    def __init__(self, destination, token):
        self._destination = destination
        self._token = token

    def query(self, endpoint, **params):
        if (
            endpoint not in ENDPOINTS
            or set(params) != {"ts_code", "start_date", "end_date", "report_type"}
            or not isinstance(params["ts_code"], str)
            or not re.fullmatch(r"\d{6}\.(SH|SZ)", params["ts_code"])
            or params["report_type"] != "1"
        ):
            raise ValueError("financial request rejected")
        start, end = (
            datetime.strptime(params[key], "%Y%m%d").date() for key in ("start_date", "end_date")
        )
        if start > end or (end - start).days > 7 * 366:
            raise ValueError("financial announcement window rejected")
        host, path = DESTINATIONS[self._destination]
        if self._destination.endswith("/dataapi"):
            path += endpoint
            params = {**params, "ts_type_name": self._destination}
        body = json.dumps(
            {"api_name": endpoint, "token": self._token, "params": params, "fields": ""},
            allow_nan=False,
        ).encode()
        connection = None
        failure, retry_after, status = None, None, None
        frame = None
        try:
            context = ssl.create_default_context(purpose=ssl.Purpose.SERVER_AUTH)
            context.minimum_version = ssl.TLSVersion.TLSv1_2
            if not context.check_hostname or context.verify_mode != ssl.CERT_REQUIRED:
                raise ValueError("verification required")
            connection = http.client.HTTPSConnection(host, timeout=30, context=context)
            connection.request(
                "POST",
                path,
                body=body,
                headers={"Content-Type": "application/json", "Connection": "close"},
            )
            response = connection.getresponse()
            status = response.status
            if status == 429:
                failure = PhysicalAttemptOutcome.RATE_LIMITED
                retry_after = _retry_after_seconds(response.getheader("Retry-After", ""))
            elif status in {401, 403}:
                failure = (
                    PhysicalAttemptOutcome.AUTHENTICATION
                    if status == 401
                    else PhysicalAttemptOutcome.PERMISSION_DENIED
                )
            elif status != 200:
                failure = PhysicalAttemptOutcome.PROVIDER_ERROR
            else:
                content = response.read(MAX_RESPONSE_BYTES + 1)
                if len(content) > MAX_RESPONSE_BYTES:
                    raise ValueError("oversized response")
                decoded = json.loads(content)
                if type(decoded) is not dict or type(decoded.get("code")) is not int:
                    raise ValueError("invalid response")
                if decoded["code"] != 0:
                    message = str(decoded.get("msg", "")).casefold()
                    if any(
                        term in message
                        for term in ("rate limit", "too many requests", "频次", "每分钟")
                    ):
                        failure = PhysicalAttemptOutcome.RATE_LIMITED
                    elif any(term in message for term in ("权限", "积分", "permission")):
                        failure = PhysicalAttemptOutcome.PERMISSION_DENIED
                    elif any(term in message for term in ("token", "认证", "authentication")):
                        failure = PhysicalAttemptOutcome.AUTHENTICATION
                    else:
                        failure = PhysicalAttemptOutcome.PROVIDER_ERROR
                else:
                    data = decoded.get("data")
                    if (
                        type(data) is not dict
                        or type(data.get("fields")) is not list
                        or not data["fields"]
                        or any(type(f) is not str for f in data["fields"])
                        or len(data["fields"]) != len(set(data["fields"]))
                        or type(data.get("items")) is not list
                        or any(
                            type(row) is not list or len(row) != len(data["fields"])
                            for row in data["items"]
                        )
                    ):
                        raise ValueError("invalid response table")
                    # Object dtype avoids implicit optional Arrow inference in
                    # pandas 3 and retains original provider scalar values.
                    frame = pd.DataFrame(data["items"], columns=data["fields"], dtype=object)
        except TimeoutError:
            failure = PhysicalAttemptOutcome.TIMEOUT
        except (http.client.RemoteDisconnected, ConnectionError):
            failure = PhysicalAttemptOutcome.DISCONNECT
        except (ValueError, TypeError, KeyError, UnicodeError):
            failure = PhysicalAttemptOutcome.MALFORMED_RESPONSE
        except Exception:
            failure = PhysicalAttemptOutcome.PROVIDER_ERROR
        finally:
            if connection is not None:
                try:
                    connection.close()
                except Exception:
                    if failure is None:
                        failure = PhysicalAttemptOutcome.DISCONNECT
        if failure is not None:
            # No raw exception or response text crosses this boundary.
            raise PhysicalAttemptFailure(
                outcome=failure,
                retryable=False,
                status_code=status,
                retry_after_seconds=(
                    (300.0 if retry_after is None else retry_after)
                    if failure is PhysicalAttemptOutcome.RATE_LIMITED
                    else None
                ),
                error_code="financial_https_" + failure.value,
            )
        return frame

"""Explicit CURRENT financial transport; no SDK default endpoint or retries."""

from __future__ import annotations

import http.client
import json
import math
import os
import re
import ssl
from contextlib import ExitStack
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
    def __init__(self, destination, token, *, before_request=None, exchange_guard=None,
                 failure_metadata=None):
        self._destination = destination
        self._token = token
        self._before_request = before_request
        self._exchange_guard = exchange_guard
        self._failure_metadata = failure_metadata

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
        error_code = None
        provider_code = None
        classification_basis = "transport"
        frame = None
        permission = ExitStack()
        try:
            context = ssl.create_default_context(purpose=ssl.Purpose.SERVER_AUTH)
            context.minimum_version = ssl.TLSVersion.TLSv1_2
            if not context.check_hostname or context.verify_mode != ssl.CERT_REQUIRED:
                raise ValueError("verification required")
            connection = http.client.HTTPSConnection(host, timeout=30, context=context)
            if self._before_request is not None:
                self._before_request(endpoint)
            if self._exchange_guard is not None:
                permission.enter_context(self._exchange_guard(connection, endpoint))
            connection.request(
                "POST",
                path,
                body=body,
                headers={"Content-Type": "application/json", "Connection": "close"},
            )
            response = connection.getresponse()
            status = response.status
            classification_basis = "http_status"
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
                classification_basis = "response_schema"
                content = response.read(MAX_RESPONSE_BYTES + 1)
                if len(content) > MAX_RESPONSE_BYTES:
                    raise ValueError("oversized response")
                decoded = json.loads(content)
                if (type(decoded) is not dict or type(decoded.get("code")) is not int
                        or not -(2 ** 31) <= decoded["code"] < 2 ** 31):
                    raise ValueError("invalid response")
                provider_code = decoded["code"]
                if provider_code != 0:
                    # No provider-code mapping has been qualified for this route.
                    # Message hints never establish that the supplied token is invalid.
                    classification_basis = "provider_code_unmapped"
                    failure = PhysicalAttemptOutcome.PROVIDER_ERROR
                    error_code = "financial_https_upstream_rejection"
                    message = decoded.get("msg")
                    message = message.casefold() if isinstance(message, str) else ""
                    if any(
                        term in message
                        for term in ("rate limit", "too many requests", "频次", "每分钟")
                    ):
                        failure = PhysicalAttemptOutcome.RATE_LIMITED
                        error_code = "financial_https_rate_limited"
                        classification_basis = "message_heuristic_rate_limit"
                    elif any(term in message for term in ("权限", "积分", "permission")):
                        classification_basis = "message_heuristic_permission"
                    elif any(term in message for term in ("token", "认证", "authentication")):
                        classification_basis = "message_heuristic_authentication"
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
        except KeyboardInterrupt:
            failure = PhysicalAttemptOutcome.PROVIDER_ERROR
            error_code = "financial_https_interrupted"
            classification_basis = "transport"
        except TimeoutError:
            failure = PhysicalAttemptOutcome.TIMEOUT
            classification_basis = "transport"
        except (http.client.RemoteDisconnected, ConnectionError):
            failure = PhysicalAttemptOutcome.DISCONNECT
            classification_basis = "transport"
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
                        classification_basis = "transport"
            permission.close()
        if failure is not None:
            # No raw exception or response text crosses this boundary.
            if self._failure_metadata is not None:
                self._failure_metadata.update(
                    provider_code=provider_code, classification_basis=classification_basis
                )
            raise PhysicalAttemptFailure(
                outcome=failure,
                retryable=False,
                status_code=status,
                retry_after_seconds=(
                    (300.0 if retry_after is None else retry_after)
                    if failure is PhysicalAttemptOutcome.RATE_LIMITED
                    else None
                ),
                error_code=error_code or "financial_https_" + failure.value,
            )
        return frame

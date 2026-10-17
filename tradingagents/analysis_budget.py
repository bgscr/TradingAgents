"""Opt-in, process-exclusive analysis admission budget.

Counts transport attempts, not tokens, invoices, logical calls or coordinator
permits. The terminal exception deliberately bypasses ordinary provider/SDK
retry handlers. A stopped budget stays stopped, even if a caller catches it.
"""

from __future__ import annotations

import math
import threading
import time
from contextlib import contextmanager
from contextvars import ContextVar


class AnalysisBudgetStopped(BaseException):
    """Terminal run control signal; never retry or turn into optional evidence."""

    def __init__(self, reason: str):
        self.reason = reason
        super().__init__(f"analysis_budget:{reason}")


class AnalysisBudget:
    def __init__(self, max_calls: int, timeout_seconds: float, *, clock=time.monotonic):
        if type(max_calls) is not int or max_calls < 1:
            raise ValueError("analysis_max_calls must be a positive integer")
        if (type(timeout_seconds) not in {int, float}
                or not math.isfinite(timeout_seconds) or timeout_seconds <= 0):
            raise ValueError("analysis_timeout_seconds must be positive and finite")
        self.max_calls = max_calls
        self.timeout_seconds = timeout_seconds
        self._clock = clock
        self._started = clock()
        self._lock = threading.RLock()
        self._counts = {"model": 0, "data": 0}
        self._reason = None
        self._cache_hits = []

    def stop(self, reason: str):
        with self._lock:
            self._reason = self._reason or reason
            raise AnalysisBudgetStopped(self._reason)

    def check(self) -> float:
        with self._lock:
            if self._reason is not None:
                raise AnalysisBudgetStopped(self._reason)
            remaining = self.timeout_seconds - (self._clock() - self._started)
            if remaining <= 0:
                self.stop("deadline_exceeded")
            return remaining

    def admit(self, kind: str) -> float:
        with self._lock:
            remaining = self.check()
            if kind not in self._counts:
                self.stop("uncovered_transport")
            if sum(self._counts.values()) >= self.max_calls:
                self.stop("call_limit_exceeded")
            self._counts[kind] += 1
            return remaining

    def record_cache_hit(self, endpoint: str, chunk) -> None:
        with self._lock:
            self.check()
            item = {
                "endpoint": endpoint,
                "announcement_start": chunk.range_start.isoformat(),
                "announcement_end": chunk.range_end.isoformat(),
                "observed_at": chunk.checked_at.isoformat(),
                "payload_sha256": chunk.payload_sha256,
            }
            if item not in self._cache_hits:
                self._cache_hits.append(item)

    def receipt(self) -> dict:
        with self._lock:
            return {
                "max_calls": self.max_calls,
                "timeout_seconds": self.timeout_seconds,
                "admitted_attempts": sum(self._counts.values()),
                "model_attempts": self._counts["model"],
                "data_attempts": self._counts["data"],
                "stop_reason": self._reason,
                "financial_cache_hits": list(self._cache_hits),
            }


_RUN_LOCK = threading.Lock()
_ACTIVE: AnalysisBudget | None = None
_UNBUDGETED_RUNS = 0
_IN_TRANSPORT: ContextVar[bool] = ContextVar("analysis_budget_transport", default=False)


def active_budget() -> AnalysisBudget | None:
    # Process ownership also covers SDK-created threads which do not copy context.
    return _ACTIVE


def transport_is_admitted() -> bool:
    return _IN_TRANSPORT.get()


def httpx_transport_modules() -> tuple:
    """Return the distinct HTTPX families supported at the physical boundary."""
    import httpx

    try:
        import httpx2
    except ModuleNotFoundError as exc:
        if exc.name != "httpx2":
            raise
        return (httpx,)
    return (httpx, httpx2)


def require_data_vendor(vendor: str) -> None:
    budget = active_budget()
    if budget is not None:
        budget.check()
        if vendor != "akshare":
            budget.stop("uncovered_data_vendor")


@contextmanager
def admitted_transport(kind: str):
    budget = active_budget()
    if budget is None:
        yield None
        return
    remaining = budget.admit(kind)
    token = _IN_TRANSPORT.set(True)
    try:
        yield remaining
        budget.check()
    finally:
        _IN_TRANSPORT.reset(token)


def _timeout(value, remaining):
    if value is None:
        return remaining
    if isinstance(value, tuple):
        return tuple(_timeout(part, remaining) for part in value)
    return min(float(value), remaining)


@contextmanager
def _transport_guards():
    """One run owns these reversible hooks; unsupported native sends fail closed."""
    import asyncio
    import socket
    import ssl
    from contextlib import ExitStack
    from unittest.mock import patch

    import requests.adapters

    original_requests = requests.adapters.HTTPAdapter.send
    original_sleep = time.sleep
    original_async_sleep = asyncio.sleep
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        internal_sockets = ()
    else:
        # Only the existing loop's own wakeup pair, never arbitrary localhost
        # traffic. These are scheduler notifications, not provider requests.
        internal_sockets = tuple(getattr(loop, name, None) for name in ("_ssock", "_csock"))

    def bounded_sleep(delay):
        original_sleep(min(delay, _ACTIVE.check()))
        _ACTIVE.check()

    async def bounded_async_sleep(delay, result=None):
        value = await original_async_sleep(min(delay, _ACTIVE.check()), result=result)
        _ACTIVE.check()
        return value

    def requests_send(adapter, request, **kwargs):
        if (type(adapter) is not requests.adapters.HTTPAdapter
                or adapter.max_retries.total not in (0, False)):
            _ACTIVE.stop("uncovered_requests_adapter")
        with admitted_transport("data") as remaining:
            kwargs["timeout"] = _timeout(kwargs.get("timeout"), remaining)
            response = original_requests(adapter, request, **kwargs)
            _ = response.content
            return response

    def check_httpx(client, request, expected):
        if getattr(client, "_tradingagents_budget_provider", None) != "deepseek":
            _ACTIVE.stop("uncovered_model_transport")
        transport = client._transport_for_url(request.url)
        if type(transport) is not expected or transport._pool._retries != 0:
            _ACTIVE.stop("uncovered_httpx_transport")

    def set_httpx_timeout(request, remaining):
        old = request.extensions.get("timeout", {})
        request.extensions["timeout"] = {
            key: _timeout(old.get(key), remaining)
            for key in ("connect", "read", "write", "pool")
        }

    def httpx_sends(module):
        # Bind each family separately; a hook must never call the other
        # family's original method or accept its transport types by accident.
        original_sync = module.Client._send_single_request
        original_async = module.AsyncClient._send_single_request

        def sync_send(client, request):
            from tradingagents.llm_clients.openai_client import _record_sync_transport_send

            check_httpx(client, request, module.HTTPTransport)
            with admitted_transport("model") as remaining:
                set_httpx_timeout(request, remaining)

                def send_and_read(req):
                    response = original_sync(client, req)
                    response.read()
                    return response

                return _record_sync_transport_send(send_and_read, request)

        async def async_send(client, request):
            from tradingagents.llm_clients.openai_client import _record_async_transport_send

            check_httpx(client, request, module.AsyncHTTPTransport)
            with admitted_transport("model") as remaining:
                set_httpx_timeout(request, remaining)

                async def send_and_read(req):
                    response = await original_async(client, req)
                    await response.aread()
                    return response

                return await _record_async_transport_send(send_and_read, request)

        return sync_send, async_send

    def guarded_socket(original):
        def call(sock, *args, **kwargs):
            if any(sock is internal for internal in internal_sockets):
                return original(sock, *args, **kwargs)
            if not _IN_TRANSPORT.get():
                _ACTIVE.stop("uncovered_socket_transport")
            remaining = _ACTIVE.check()
            previous = sock.gettimeout()
            # Nonblocking async sockets must remain nonblocking.
            if previous != 0:
                sock.settimeout(_timeout(previous, remaining))
            try:
                return original(sock, *args, **kwargs)
            finally:
                if previous != 0 and sock.fileno() >= 0:
                    sock.settimeout(previous)
        return call

    def reject_native(*args, **kwargs):
        _ACTIVE.stop("uncovered_native_transport")

    with ExitStack() as stack:
        stack.enter_context(patch.object(requests.adapters.HTTPAdapter, "send", requests_send))
        for module in httpx_transport_modules():
            sync_send, async_send = httpx_sends(module)
            stack.enter_context(patch.object(module.Client, "_send_single_request", sync_send))
            stack.enter_context(patch.object(module.AsyncClient, "_send_single_request", async_send))
        stack.enter_context(patch.object(time, "sleep", bounded_sleep))
        stack.enter_context(patch.object(asyncio, "sleep", bounded_async_sleep))
        for cls in (socket.socket, ssl.SSLSocket):
            for name in ("connect", "connect_ex", "send", "sendall", "sendto", "recv", "recv_into"):
                stack.enter_context(patch.object(cls, name, guarded_socket(getattr(cls, name))))
        # Yahoo/curl uses native I/O, outside Python's socket boundary.
        try:
            import curl_cffi
        except ImportError:
            pass
        else:
            stack.enter_context(patch.object(curl_cffi.Curl, "perform", reject_native))
            stack.enter_context(patch.object(curl_cffi.AsyncCurl, "add_handle", reject_native))
        yield


def validate_budget_config(config: dict) -> None:
    enabled = config.get("analysis_budget_enabled", False)
    cache_only = config.get("financial_cache_only", False)
    if type(enabled) is not bool or type(cache_only) is not bool:
        raise ValueError("analysis budget/cache-only flags must be boolean")
    if cache_only and config.get("tushare_financial_force_refresh", False):
        raise ValueError("financial_cache_only conflicts with force refresh")
    if cache_only and (
        config.get("market_history_mode") != "current"
        or config.get("mainland_capability_routing_mode", "legacy") != "legacy"
        or config.get("checkpoint_enabled", False)
        or any(config.get("tool_vendors", {}).get(tool) != "tushare"
               for tool in ("get_balance_sheet", "get_income_statement", "get_cashflow"))
    ):
        raise ValueError("cache-only requires explicit CURRENT Tushare statements, legacy, no checkpoint")
    if enabled:
        AnalysisBudget(config.get("analysis_max_calls", 30),
                       config.get("analysis_timeout_seconds", 600))
        if (str(config.get("llm_provider", "")).lower() != "deepseek"
                or config.get("market_history_mode") != "current"
                or config.get("mainland_capability_routing_mode", "legacy") != "legacy"
                or config.get("checkpoint_enabled", False)):
            raise ValueError("budget requires DeepSeek, CURRENT, legacy routing, no checkpoint")


@contextmanager
def analysis_budget_scope(config: dict):
    global _ACTIVE, _UNBUDGETED_RUNS
    validate_budget_config(config)
    if not config.get("analysis_budget_enabled", False):
        with _RUN_LOCK:
            if _ACTIVE is not None:
                raise AnalysisBudgetStopped("concurrent_analysis_unsupported")
            _UNBUDGETED_RUNS += 1
        try:
            yield None
        finally:
            with _RUN_LOCK:
                _UNBUDGETED_RUNS -= 1
        return
    with _RUN_LOCK:
        if _ACTIVE is not None or _UNBUDGETED_RUNS:
            raise AnalysisBudgetStopped("concurrent_analysis_unsupported")
        budget = AnalysisBudget(config.get("analysis_max_calls", 30),
                                config.get("analysis_timeout_seconds", 600))
        _ACTIVE = budget
    try:
        with _transport_guards():
            yield budget
            budget.check()
    finally:
        with _RUN_LOCK:
            _ACTIVE = None

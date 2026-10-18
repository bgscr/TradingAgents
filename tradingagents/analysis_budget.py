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


def _valid_timeout(value) -> bool:
    if type(value) not in {int, float}:
        return False
    try:
        return math.isfinite(value) and value > 0
    except OverflowError:
        return False


class AnalysisBudget:
    def __init__(self, max_calls: int, timeout_seconds: float, *, clock=time.monotonic,
                 disable_alpha_vantage: bool = False):
        if type(max_calls) is not int or max_calls < 1:
            raise ValueError("analysis_max_calls must be a positive integer")
        if not _valid_timeout(timeout_seconds):
            raise ValueError("analysis_timeout_seconds must be positive and finite")
        self.max_calls = max_calls
        self.timeout_seconds = timeout_seconds
        self._clock = clock
        self._started = clock()
        self._lock = threading.RLock()
        self._counts = {"model": 0, "data": 0}
        self._reason = None
        self._cache_hits = []
        self.disable_alpha_vantage = disable_alpha_vantage
        self._data_gaps = []

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

    def record_data_gap(self, tool: str, reason: str) -> str:
        """Operational diagnostics only; never source facts or usable data."""
        with self._lock:
            self.check()
            item = {"tool": tool, "provider": "alpha_vantage", "reason": reason}
            if item not in self._data_gaps:
                self._data_gaps.append(item)
        return (
            f"DATA_UNAVAILABLE: {tool}; provider=alpha_vantage; reason={reason}. "
            "No cross-provider fallback. Report this gap; do not treat it as empty "
            "news, absence of transactions, or evidence for a factual claim."
        )

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
                "data_gaps": [dict(item) for item in self._data_gaps],
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
        from tradingagents.budget_data import check_budget_data_destination

        check_budget_data_destination(request)
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


def inspect_budget_config(config: dict) -> tuple[tuple[str, str], ...]:
    """Collect fixed-safe configuration issues without constructing a budget."""
    issues = []
    enabled = config.get("analysis_budget_enabled", False)
    cache_only = config.get("financial_cache_only", False)
    for name, value in (("analysis_budget_enabled", enabled), ("financial_cache_only", cache_only)):
        if type(value) is not bool:
            issues.append((name, "must_be_boolean"))
    disabled = config.get("analysis_disable_alpha_vantage", False)
    if type(disabled) is not bool:
        issues.append(("analysis_disable_alpha_vantage", "must_be_boolean"))
    elif disabled and enabled is not True:
        issues.append(("analysis_disable_alpha_vantage", "requires_analysis_budget"))
    if cache_only is not False:
        if config.get("tushare_financial_force_refresh", False):
            issues.append(("financial_cache_only", "force_refresh_conflict"))
        tools = config.get("tool_vendors", {})
        if not isinstance(tools, dict):
            issues.append(("tool_vendors", "invalid_mapping"))
        else:
            for tool in ("get_balance_sheet", "get_income_statement", "get_cashflow"):
                if tools.get(tool) != "tushare":
                    issues.append(("tool_vendors." + tool, "cache_only_requires_explicit_tushare"))
    if enabled is not False or cache_only is not False:
        for name, expected in (("market_history_mode", "current"),
                               ("mainland_capability_routing_mode", "legacy"),
                               ("checkpoint_enabled", False)):
            if config.get(name, expected if name != "market_history_mode" else None) != expected:
                issues.append((name, "requires_" + str(expected).lower()))
    if enabled is not False:
        maximum = config.get("analysis_max_calls", 30)
        timeout = config.get("analysis_timeout_seconds", 600)
        if type(maximum) is not int or maximum < 1:
            issues.append(("analysis_max_calls", "must_be_positive_integer"))
        if not _valid_timeout(timeout):
            issues.append(("analysis_timeout_seconds", "must_be_positive_finite"))
        if str(config.get("llm_provider", "")).lower() != "deepseek":
            issues.append(("llm_provider", "requires_deepseek"))
    return tuple(issues)


def validate_budget_config(config: dict) -> None:
    issues = inspect_budget_config(config)
    if issues:
        raise ValueError("analysis_budget:invalid_config; " + "; ".join(
            field + ":" + reason for field, reason in issues
        ))


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
                                config.get("analysis_timeout_seconds", 600),
                                disable_alpha_vantage=config.get("analysis_disable_alpha_vantage", False))
        _ACTIVE = budget
    try:
        with _transport_guards():
            yield budget
            budget.check()
    finally:
        with _RUN_LOCK:
            _ACTIVE = None

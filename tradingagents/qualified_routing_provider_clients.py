"""Run-owned provider clients used by the qualified routing composition."""

from __future__ import annotations

import importlib
import os
import threading
from collections.abc import Mapping
from contextlib import suppress

from tradingagents.market_history.coordinator import (
    PhysicalAttemptFailure,
    PhysicalAttemptOutcome,
)
from tradingagents.qualified_routing_lifecycle import (
    ProductionQualifiedRoutingLifecycleError,
    ProductionQualifiedRoutingLifecycleFailureReason,
)


class _ProductionRunModuleFacade:
    """Run-owned module facade that never stores credentials or raw failures."""

    def __init__(
        self,
        *,
        provider_id: str,
        module: object,
        stop_signal: threading.Event,
    ) -> None:
        self._provider_id = provider_id
        self._module = module
        self._stop_signal = stop_signal
        self._lock = threading.RLock()
        self._closed = False

    def __getattr__(self, name: str) -> object:
        with self._lock:
            if self._closed or self._stop_signal.is_set():
                raise ProductionQualifiedRoutingLifecycleError(
                    ProductionQualifiedRoutingLifecycleFailureReason.INTERRUPTED
                )
            attribute = getattr(self._module, name)
        if not callable(attribute):
            return attribute

        def invoke(*args: object, **kwargs: object) -> object:
            with self._lock:
                if self._closed or self._stop_signal.is_set():
                    raise ProductionQualifiedRoutingLifecycleError(
                        ProductionQualifiedRoutingLifecycleFailureReason.INTERRUPTED
                    )
            return attribute(*args, **kwargs)

        return invoke

    def interrupt(self) -> None:
        self._stop_signal.set()

    def close(self) -> None:
        with self._lock:
            if self._closed:
                return
            self._closed = True
            close = getattr(self._module, "close", None)
        if callable(close):
            close()

    def __repr__(self) -> str:
        return f"ProductionRunModuleFacade(provider_id={self._provider_id!r})"


class _ProductionTushareRunClient(_ProductionRunModuleFacade):
    def __init__(
        self,
        *,
        module: object,
        credential: str,
        stop_signal: threading.Event,
    ) -> None:
        pro_api = getattr(module, "pro_api", None)
        if not callable(pro_api) or not credential:
            raise ProductionQualifiedRoutingLifecycleError(
                ProductionQualifiedRoutingLifecycleFailureReason.BINDING_INVALID
            )
        try:
            client = pro_api(credential)
        except Exception:
            raise ProductionQualifiedRoutingLifecycleError(
                ProductionQualifiedRoutingLifecycleFailureReason.BINDING_INVALID
            ) from None
        super().__init__(
            provider_id="tushare",
            module=client,
            stop_signal=stop_signal,
        )


def production_tushare_run_client(
    stop_signal: threading.Event,
) -> _ProductionTushareRunClient:
    """Create one run-scoped Tushare client through the environment boundary."""

    try:
        return _ProductionTushareRunClient(
            module=importlib.import_module("tushare"),
            credential=os.environ.get("TUSHARE_TOKEN", ""),
            stop_signal=stop_signal,
        )
    except ProductionQualifiedRoutingLifecycleError:
        raise
    except Exception:
        raise ProductionQualifiedRoutingLifecycleError(
            ProductionQualifiedRoutingLifecycleFailureReason.BINDING_INVALID
        ) from None


def production_module_run_facade(
    *,
    provider_id: str,
    module_name: str,
    stop_signal: threading.Event,
) -> _ProductionRunModuleFacade:
    """Create one run-owned facade without mutating provider module state."""

    try:
        module = importlib.import_module(module_name)
    except Exception:
        raise ProductionQualifiedRoutingLifecycleError(
            ProductionQualifiedRoutingLifecycleFailureReason.BINDING_INVALID
        ) from None
    return _ProductionRunModuleFacade(
        provider_id=provider_id,
        module=module,
        stop_signal=stop_signal,
    )


def _baostock_result_rows(
    result: object,
) -> tuple[tuple[str, ...], tuple[tuple[object, ...], ...]]:
    error_code = str(getattr(result, "error_code", "0"))
    if error_code not in {"0", "00", ""}:
        raise ValueError("baostock_result_failed")
    if hasattr(result, "columns") and hasattr(result, "itertuples"):
        fields = tuple(str(value) for value in result.columns)
        rows = tuple(
            tuple(value for value in row)
            for row in result.itertuples(index=False, name=None)
        )
        return fields, rows
    fields = tuple(str(value) for value in getattr(result, "fields", ()))
    next_row = getattr(result, "next", None)
    get_row_data = getattr(result, "get_row_data", None)
    if not fields or not callable(next_row) or not callable(get_row_data):
        raise ValueError("baostock_result_malformed")
    collected: list[tuple[object, ...]] = []
    while next_row():
        collected.append(tuple(get_row_data()))
    return fields, tuple(collected)


class _BaoStockEndpointFacade:
    """Shared typed endpoint surface for process and deterministic transports."""

    def _query(self, method_name: str, **kwargs: object):
        raise NotImplementedError

    def query_raw_daily(self, **kwargs: object):
        return self._query(
            "query_history_k_data_plus",
            fields="date,code,open,high,low,close,preclose,volume,amount",
            frequency="d",
            adjustflag="3",
            **kwargs,
        )

    def query_forward_adjustment_factors(self, **kwargs: object):
        return self._query("query_adjust_factor", **kwargs)

    def query_session_status(self, **kwargs: object):
        return self._query(
            "query_history_k_data_plus",
            fields="date,code,preclose,tradestatus,isST",
            frequency="d",
            adjustflag="3",
            **kwargs,
        )

    def query_lifecycle(self, **kwargs: object):
        return self._query("query_stock_basic", **kwargs)

    def query_profit_data(self, **kwargs: object):
        return self._query("query_profit_data", **kwargs)

    def query_operation_data(self, **kwargs: object):
        return self._query("query_operation_data", **kwargs)

    def query_growth_data(self, **kwargs: object):
        return self._query("query_growth_data", **kwargs)

    def query_balance_data(self, **kwargs: object):
        return self._query("query_balance_data", **kwargs)

    def query_cash_flow_data(self, **kwargs: object):
        return self._query("query_cash_flow_data", **kwargs)

    def query_dupont_data(self, **kwargs: object):
        return self._query("query_dupont_data", **kwargs)


def _execute_baostock_worker_query(
    method: object,
    kwargs: Mapping[str, object],
    query_result: object,
    completed: threading.Event,
) -> None:
    try:
        if not callable(method):  # pragma: no cover - worker validates before spawn
            raise TypeError("baostock endpoint is unavailable")
        fields, rows = _baostock_result_rows(method(**dict(kwargs)))
        query_result.put(("ok", fields, rows))
    except BaseException:
        query_result.put(("failure", "baostock_provider_error"))
    finally:
        completed.set()


def _baostock_run_worker(request_queue: object, response_queue: object) -> None:
    """Own the BaoStock SDK's module session inside one run-isolated process."""

    logged_in = False
    login_attempted = False
    logout_complete = False
    sdk_module = None

    def logout_once() -> None:
        nonlocal logged_in, logout_complete
        if sdk_module is None or logout_complete or not (logged_in or login_attempted):
            return
        logout_complete = True
        try:
            logout = getattr(sdk_module, "logout", None)
            if callable(logout):
                logout()
        finally:
            logged_in = False

    try:
        sdk_module = importlib.import_module("baostock")
        while True:
            command = request_queue.get()
            if not isinstance(command, tuple) or not command:
                response_queue.put(("failure", "baostock_command_invalid"))
                break
            if command[0] == "close":
                break
            if command[0] != "query" or len(command) != 3:
                response_queue.put(("failure", "baostock_command_invalid"))
                break
            if not logged_in:
                login_attempted = True
                login = getattr(sdk_module, "login", None)
                if not callable(login):
                    response_queue.put(("failure", "baostock_login_unavailable"))
                    break
                login_result = login()
                login_code = str(getattr(login_result, "error_code", "0"))
                if login_code not in {"0", "00", ""}:
                    response_queue.put(("failure", "baostock_login_failed"))
                    break
                logged_in = True
            method_name = str(command[1])
            kwargs = command[2]
            method = getattr(sdk_module, method_name, None)
            if not callable(method) or not isinstance(kwargs, Mapping):
                response_queue.put(("failure", "baostock_endpoint_unavailable"))
                break

            from queue import Empty, Queue

            completed = threading.Event()
            query_result: Queue[tuple[object, ...]] = Queue(maxsize=1)
            query_thread = threading.Thread(
                target=_execute_baostock_worker_query,
                args=(method, dict(kwargs), query_result, completed),
                name="qualified-baostock-query",
            )
            query_thread.start()
            interrupted = False
            while not completed.is_set():
                try:
                    control = request_queue.get(timeout=0.05)
                except Empty:
                    continue
                if control == ("close",):
                    response_queue.put(("closing",))
                    interrupted = True
                    break
                response_queue.put(("failure", "baostock_command_invalid"))
                interrupted = True
                break
            if interrupted:
                query_thread.join()
                break
            query_thread.join()
            response = query_result.get()
            response_queue.put(response)
            if response[0] != "ok":
                break
    except BaseException:
        with suppress(BaseException):
            response_queue.put(("failure", "baostock_provider_error"))
    finally:
        with suppress(BaseException):
            logout_once()


class _ProductionBaoStockProcessTransport(_BaoStockEndpointFacade):
    """Lazy run-isolated BaoStock process, queues, session, and cleanup owner."""

    def __init__(self, *, stop_signal: threading.Event) -> None:
        self._stop_signal = stop_signal
        self._lock = threading.RLock()
        self._process = None
        self._request_queue = None
        self._response_queue = None
        self._terminal = False
        self._interrupted = False
        self._closed = False
        self._forced_shutdown = False
        self._forced_shutdown_reported = False

    @property
    def worker_alive(self) -> bool:
        with self._lock:
            return bool(self._process is not None and self._process.is_alive())

    def _raise_abandoned(self) -> None:
        raise PhysicalAttemptFailure(
            outcome=PhysicalAttemptOutcome.ABANDONED,
            retryable=False,
            error_code="run_interrupted",
        )

    def _ensure_worker_locked(self) -> None:
        if (
            self._closed
            or self._interrupted
            or self._terminal
            or self._stop_signal.is_set()
        ):
            self._raise_abandoned()
        if self._process is not None:
            return
        from multiprocessing import get_context

        context = get_context("spawn")
        request_queue = context.Queue()
        response_queue = context.Queue()
        process = context.Process(
            target=_baostock_run_worker,
            args=(request_queue, response_queue),
            name="qualified-baostock-run",
        )
        self._request_queue = request_queue
        self._response_queue = response_queue
        self._process = process
        try:
            process.start()
        except BaseException as exc:
            self._terminal = True
            self._shutdown_locked(force=True)
            if not isinstance(exc, Exception) or isinstance(
                exc,
                (KeyboardInterrupt, SystemExit),
            ):
                raise
            raise PhysicalAttemptFailure(
                outcome=PhysicalAttemptOutcome.PROVIDER_ERROR,
                retryable=False,
                error_code="baostock_worker_unavailable",
            ) from None

    def _shutdown_locked(self, *, force: bool) -> None:
        process = self._process
        request_queue = self._request_queue
        response_queue = self._response_queue
        self._process = None
        self._request_queue = None
        self._response_queue = None
        if process is not None:
            if process.is_alive() and not force and request_queue is not None:
                try:
                    request_queue.put(("close",))
                except BaseException:
                    force = True
            if process.pid is not None:
                process.join(timeout=5)
                if process.is_alive():
                    self._forced_shutdown = True
                    process.terminate()
                    process.join(timeout=5)
                if process.is_alive():
                    self._forced_shutdown = True
                    kill = getattr(process, "kill", None)
                    if callable(kill):
                        kill()
                        process.join(timeout=5)
        for owned_queue in (request_queue, response_queue):
            if owned_queue is None:
                continue
            try:
                owned_queue.close()
                owned_queue.join_thread()
            except BaseException:
                pass
        if process is not None and process.pid is not None and process.is_alive():
            raise ProductionQualifiedRoutingLifecycleError(
                ProductionQualifiedRoutingLifecycleFailureReason.INTERRUPTED
            )

    def _report_forced_shutdown_once_locked(self) -> None:
        if self._forced_shutdown and not self._forced_shutdown_reported:
            self._forced_shutdown_reported = True
            raise ProductionQualifiedRoutingLifecycleError(
                ProductionQualifiedRoutingLifecycleFailureReason.INTERRUPTED
            )

    def _query(self, method_name: str, **kwargs: object):
        from queue import Empty

        import pandas as pd

        with self._lock:
            self._ensure_worker_locked()
            assert self._request_queue is not None
            assert self._response_queue is not None
            assert self._process is not None
            self._request_queue.put(("query", method_name, dict(kwargs)))
            while True:
                if self._stop_signal.is_set():
                    self._interrupted = True
                    self._shutdown_locked(force=False)
                    self._raise_abandoned()
                try:
                    response = self._response_queue.get(timeout=0.05)
                except Empty:
                    if not self._process.is_alive():
                        self._terminal = True
                        self._shutdown_locked(force=True)
                        raise PhysicalAttemptFailure(
                            outcome=PhysicalAttemptOutcome.PROVIDER_ERROR,
                            retryable=False,
                            error_code="baostock_worker_unavailable",
                        ) from None
                    continue
                if (
                    not isinstance(response, tuple)
                    or not response
                    or response[0] != "ok"
                ):
                    self._terminal = True
                    self._shutdown_locked(force=False)
                    error_code = (
                        str(response[1])
                        if isinstance(response, tuple) and len(response) == 2
                        else "baostock_provider_error"
                    )
                    if not error_code.isidentifier():
                        error_code = "baostock_provider_error"
                    raise PhysicalAttemptFailure(
                        outcome=PhysicalAttemptOutcome.PROVIDER_ERROR,
                        retryable=False,
                        error_code=error_code,
                    )
                return pd.DataFrame(response[2], columns=response[1])

    def interrupt(self) -> None:
        self._stop_signal.set()
        with self._lock:
            if self._interrupted:
                self._report_forced_shutdown_once_locked()
                return
            self._interrupted = True
            self._shutdown_locked(force=False)
            self._report_forced_shutdown_once_locked()

    def close(self) -> None:
        with self._lock:
            if self._closed:
                self._report_forced_shutdown_once_locked()
                return
            self._closed = True
            self._shutdown_locked(force=False)
            self._report_forced_shutdown_once_locked()

    def __repr__(self) -> str:
        return "ProductionBaoStockProcessTransport(run_owned=True)"


def production_baostock_process_transport(
    stop_signal: threading.Event,
) -> _ProductionBaoStockProcessTransport:
    """Create a lazy run-owned BaoStock process transport."""

    return _ProductionBaoStockProcessTransport(stop_signal=stop_signal)


class ProductionBaoStockRunTransport(_BaoStockEndpointFacade):
    """One lazy, serialized BaoStock login and endpoint facade for one run."""

    def __init__(
        self,
        *,
        sdk_module: object,
        stop_signal: threading.Event,
    ) -> None:
        self._sdk_module = sdk_module
        self._stop_signal = stop_signal
        self._lock = threading.RLock()
        self._logged_in = False
        self._logout_complete = False
        self._terminal = False
        self._closed = False

    @property
    def logged_in(self) -> bool:
        with self._lock:
            return self._logged_in

    def _raise_abandoned(self) -> None:
        raise PhysicalAttemptFailure(
            outcome=PhysicalAttemptOutcome.ABANDONED,
            retryable=False,
            error_code="run_interrupted",
        )

    def _logout_locked(self) -> None:
        if not self._logged_in or self._logout_complete:
            return
        self._logout_complete = True
        try:
            logout = getattr(self._sdk_module, "logout", None)
            if callable(logout):
                logout()
        finally:
            self._logged_in = False

    def _terminal_failure_locked(self, error_code: str) -> None:
        self._terminal = True
        self._logout_locked()
        raise PhysicalAttemptFailure(
            outcome=PhysicalAttemptOutcome.PROVIDER_ERROR,
            retryable=False,
            error_code=error_code,
        )

    def _ensure_login_locked(self) -> None:
        if self._closed or self._terminal or self._stop_signal.is_set():
            self._raise_abandoned()
        if self._logged_in:
            return
        login = getattr(self._sdk_module, "login", None)
        if not callable(login):
            self._terminal_failure_locked("baostock_login_unavailable")
        try:
            result = login()
        except Exception:
            self._terminal_failure_locked("baostock_login_failed")
        error_code = str(getattr(result, "error_code", "0"))
        if error_code not in {"0", "00", ""}:
            self._terminal_failure_locked("baostock_login_failed")
        self._logged_in = True
        self._logout_complete = False
        if self._stop_signal.is_set():
            self._logout_locked()
            self._raise_abandoned()

    def _query(self, method_name: str, **kwargs: object):
        import pandas as pd

        with self._lock:
            self._ensure_login_locked()
            method = getattr(self._sdk_module, method_name, None)
            if not callable(method):
                self._terminal_failure_locked("baostock_endpoint_unavailable")
            try:
                fields, rows = _baostock_result_rows(method(**kwargs))
                frame = pd.DataFrame(rows, columns=fields)
            except PhysicalAttemptFailure:
                raise
            except Exception:
                self._terminal_failure_locked("baostock_provider_error")
            if self._stop_signal.is_set():
                self._logout_locked()
                self._raise_abandoned()
            return frame

    def interrupt(self) -> None:
        self._stop_signal.set()
        with self._lock:
            if self._terminal and self._logout_complete:
                return
            self._terminal = True
            self._logout_locked()

    def close(self) -> None:
        with self._lock:
            if self._closed:
                return
            self._closed = True
            self._logout_locked()

    def __repr__(self) -> str:
        return "ProductionBaoStockRunTransport(run_owned=True)"


__all__ = [
    "ProductionBaoStockRunTransport",
    "production_baostock_process_transport",
    "production_module_run_facade",
    "production_tushare_run_client",
]

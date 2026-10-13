"""Bounded, user-started CURRENT data acceptance. Importing this file does no IO.

This is an application tripwire for the reviewed Python path, not an OS sandbox
or native qualification. No model graph is constructed. A ledger is exclusive:
restarting the command cannot resume or reset an existing acceptance run.
"""
from __future__ import annotations

import math
import os
import sys
import threading
import time
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone

LIMIT = 24
BASE = "https://api.waditu.com/dataapi"
SYMBOLS = ("600895.SS", "000001.SZ")
REQUEST_SYMBOLS = {"600895.SH", "000001.SZ"}
FIELDS = {
    "daily": "ts_code,trade_date,open,high,low,close,pre_close,change,pct_chg,vol,amount",
    "adj_factor": "ts_code,trade_date,adj_factor",
    "suspend_d": "ts_code,trade_date,suspend_timing,suspend_type",
    "trade_cal": "exchange,cal_date,is_open,pretrade_date",
}
# Diagnostic vocabulary only; these names grant no additional audit permission.
AUDIT_NAMES = frozenset({
    "socket.__new__", "socket.bind", "socket.connect", "socket.getaddrinfo",
    "socket.gethostbyaddr", "socket.gethostbyname", "socket.gethostname",
    "socket.getnameinfo", "socket.sendto", "http.client.connect", "http.client.send",
    "urllib.Request", "subprocess.Popen", "os.spawn", "os.posix_spawn", "os.system",
    "os.fork", "os.exec", "os.startfile", "open", "sqlite3.connect",
    "sqlite3.enable_load_extension", "sqlite3.load_extension", "os.remove", "os.rmdir",
    "os.mkdir", "os.chmod", "os.utime", "os.truncate", "os.rename", "os.link", "os.symlink",
})
STAGES = frozenset({"bootstrap", "application_imports", "runtime_configuration",
                    "calendar_discovery", "data_phases"})


@contextmanager
def local_platform_metadata():
    """Process metadata for this dedicated data run, without host probes.

    Windows platform.uname normally queries hostname and may use WMI or a
    subprocess. Dependencies need only OS/architecture flags here. Report the
    interpreter's architecture (not native host architecture under emulation),
    the kernel version, and blanks for unqueried host/CPU/marketing details.
    No platform query, environment credential access or permission is added.
    """
    import platform
    import socket

    ipv6 = socket.has_ipv6
    original_uname, original_win32_ver = platform.uname, platform.win32_ver
    # platform.platform caches the adapted result; restore that cache as well.
    original_platform_cache = platform._platform_cache
    try:
        socket.has_ipv6 = False  # Avoid urllib3's optional local socket probe.
        if sys.platform == "win32":
            import sysconfig
            from collections import namedtuple

            machine = {"win32": "x86", "win-amd64": "AMD64", "win-arm32": "ARM",
                       "win-arm64": "ARM64"}.get(sysconfig.get_platform())
            if machine is None:
                raise AcceptanceStop("unsupported_process_platform")
            windows = sys.getwindowsversion()
            # major/minor/build may describe a Windows compatibility mode.
            version = ".".join(str(part) for part in windows.platform_version)
            result = namedtuple("uname_result", "system node release version machine processor")(
                "Windows", "", "", version, machine, "")
            platform.uname = lambda: result
            platform.win32_ver = lambda release="", version="", csd="", ptype="": (
                "", result.version, "", "")
            platform._platform_cache = {}
        yield
    finally:
        socket.has_ipv6 = ipv6
        platform.uname, platform.win32_ver = original_uname, original_win32_ver
        platform._platform_cache = original_platform_cache


class AcceptanceStop(BaseException):
    """Fatal stop that ordinary provider fallback must not swallow."""

    def __init__(self, code, *, retry_after_seconds=None):
        super().__init__(code)
        self.retry_after_seconds = retry_after_seconds
        self.retry_after = retry_after_seconds
        self.status_code = 429 if retry_after_seconds is not None else None
        self.retryable = False


def five_year_start(end: date) -> date:
    try:
        return end.replace(year=end.year - 5)
    except ValueError:
        return end.replace(year=end.year - 5, day=28)


def utc_now():
    return datetime.now(timezone.utc)


@dataclass(frozen=True)
class CalendarDiscoveryWindow:
    """A run's fixed 31 prior natural days; midnight never expands its scope."""

    started_at: datetime
    run_date: date
    start: date
    end: date
    requested_end: date | None

    @classmethod
    def capture(cls, requested_end=None, *, started_at=None):
        instant = started_at if started_at is not None else utc_now()
        if instant.tzinfo is None or instant.utcoffset() is None:
            raise ValueError("acceptance start must be timezone aware")
        today = instant.astimezone(timezone(timedelta(hours=8))).date()
        first, last = today - timedelta(days=31), today - timedelta(days=1)
        if requested_end is not None and (
            type(requested_end) is not date or not first <= requested_end <= last
        ):
            raise AcceptanceStop("explicit_end_outside_recent_calendar_window")
        return cls(instant.astimezone(timezone.utc), today, first, last, requested_end)


class RequestGuard:
    """One physical-send owner, shared by both stocks and the reopen phase."""

    def __init__(self, start, end, record, *, now=time.monotonic, sleep=time.sleep, discovery=None):
        self.start = date.fromisoformat(start)
        self.end = date.fromisoformat(end)
        if self.start > self.end:
            raise ValueError("invalid acceptance window")
        if discovery is not None and (self.start, self.end) != (discovery.start, discovery.end):
            raise ValueError("calendar discovery window mismatch")
        self.discovery = discovery
        self.discovering = discovery is not None
        self.record, self.now, self.sleep = record, now, sleep
        self.attempts = 0
        self.stopped = None
        self.reusing = False
        self.last_send = None
        self.lock = threading.RLock()
        self.local = threading.local()
        self.stage = "bootstrap"
        self.rejection = {}

    def stop(self, code, *, event=None):
        if not self.stopped and event is not None:
            self.rejection = {
                "audit_event": event if event in AUDIT_NAMES else "other_audit_event",
                "stage": self.stage if self.stage in STAGES else "unknown",
            }
        self.stopped = self.stopped or code
        raise AcceptanceStop(self.stopped) from None

    def ready(self):
        if self.stopped:
            raise AcceptanceStop(self.stopped) from None
        if self.reusing:
            self.stop("reuse_network_forbidden")
        if self.attempts >= LIMIT:
            self.stop("physical_budget_exhausted")

    @contextmanager
    def cache_only(self):
        with self.lock:
            self.reusing = True
        try:
            yield
        finally:
            with self.lock:
                self.reusing = False

    def coordinated(self, original, owner, *, expected_service, **kwargs):
        with self.lock:
            self.ready()
            endpoint = kwargs.get("operation")
            if endpoint not in FIELDS or kwargs.get("upstream_service_id") != expected_service:
                self.stop("unexpected_provider_or_endpoint")
        physical = self.ticketed(endpoint, kwargs["physical_request"])
        return self.finish_coordinated(original, owner, {**kwargs, "physical_request": physical})

    def ticketed(self, endpoint, physical):
        if endpoint not in FIELDS:
            self.stop("unexpected_provider_or_endpoint")
        invoked = False

        def one_attempt():
            nonlocal invoked
            with self.lock:
                self.ready()
                if invoked or getattr(self.local, "ticket", None) is not None:
                    self.stop("retry_or_nested_request")
                invoked = True
                self.local.ticket = {"endpoint": endpoint, "used": False}
            try:
                return physical()
            finally:
                self.local.ticket = None

        return one_attempt

    def finish_coordinated(self, original, owner, kwargs):

        try:
            result = original(owner, **kwargs)
        except BaseException:
            if self.stopped:
                raise AcceptanceStop(self.stopped) from None
            raise
        if self.stopped:
            raise AcceptanceStop(self.stopped) from None
        return result

    def coordinated_workflow(self, original, owner, *, expected_service, **kwargs):
        with self.lock:
            self.ready()
            if kwargs.get("upstream_service_id") != expected_service:
                self.stop("unexpected_provider_or_endpoint")
        workflow = kwargs["workflow"]

        def bounded_workflow(execute_operation):
            def execute(endpoint, physical):
                # The ticket is issued only inside the coordinator's permitted
                # physical callback, after lease acquisition and pacing.
                return execute_operation(endpoint, self.ticketed(endpoint, physical))
            return workflow(execute)

        return self.finish_coordinated(original, owner, {**kwargs, "workflow": bounded_workflow})

    def validate(self, url, payload, timeout, extra):
        ticket = getattr(self.local, "ticket", None)
        if ticket is None or ticket["used"]:
            self.stop("uncoordinated_send")
        endpoint = ticket["endpoint"]
        if (extra or url != BASE + "/" + endpoint or type(payload) is not dict
                or set(payload) != {"api_name", "token", "params", "fields"}
                or payload["api_name"] != endpoint or payload["fields"] != FIELDS[endpoint]
                or type(payload["params"]) is not dict
                or type(timeout) not in (int, float) or not 0 < timeout <= 30):
            self.stop("request_shape_rejected")
        params = payload["params"]
        allowed = {"start_date", "end_date", "limit", "offset", "ts_type_name"}
        allowed.add("exchange" if endpoint == "trade_cal" else "ts_code")
        if set(params) - allowed or params.get("ts_type_name") != BASE:
            self.stop("request_scope_rejected")
        if endpoint == "trade_cal":
            if params.get("exchange") not in {"SSE", "SZSE"}:
                self.stop("exchange_rejected")
        elif params.get("ts_code") not in REQUEST_SYMBOLS:
            self.stop("instrument_rejected")
        for key in ("offset", "limit"):
            if type(params.get(key)) is not int:
                self.stop("pagination_rejected")
        if not 1 <= params["limit"] <= 5000 or params["offset"] < 0:
            self.stop("pagination_rejected")
        try:
            end = datetime.strptime(params["end_date"], "%Y%m%d").date()
            start_value = params.get("start_date")
            start = datetime.strptime(start_value, "%Y%m%d").date() if start_value else None
        except (KeyError, TypeError, ValueError):
            self.stop("date_rejected")
        if self.discovering and (
            endpoint != "trade_cal" or (start, end) != (self.discovery.start, self.discovery.end)
        ):
            self.stop("calendar_discovery_scope_rejected")
        if (end > self.end or end < self.start or (start is not None and start > end)
                or (endpoint != "adj_factor" and (start is None or start < self.start))):
            self.stop("date_rejected")
        return ticket, endpoint, params

    def post(self, url, *, json, timeout=30, **kwargs):
        # Product sender and decoder are retained; only the pre-send boundary is
        # replaced. No Requests/global SDK URL or installed file is changed.
        with self.lock:
            self.ready()
            ticket, endpoint, params = self.validate(url, json, timeout, kwargs)
            if self.last_send is not None:
                self.sleep(max(0.0, 1.5 - (self.now() - self.last_send)))
            ticket["used"] = True
            self.attempts += 1
            event = {"kind": "physical_attempt", "ordinal": self.attempts,
                     "endpoint": endpoint, "offset": params["offset"],
                     "start_date": params.get("start_date"), "end_date": params["end_date"]}
            try:
                # Durable reservation precedes transport. Failure is never refunded.
                self.record(event)
                self.last_send = self.now()
                self.local.sending = True
                return self.transport.post(url, json=json, timeout=timeout)
            except BaseException as exc:
                self.stopped = "transport_or_ledger_failure"
                delay = getattr(exc, "retry_after", None)
                failure_type = getattr(self, "transport_failure_type", ())
                if isinstance(exc, failure_type) and exc.status_code == 429:
                    if not (type(delay) in (int, float) and math.isfinite(delay) and delay >= 0):
                        delay = None
                    # Keep the product's typed 429 path alive until coordinator
                    # cooldown/lease cleanup finishes. The fatal latch prevents
                    # another send and finish_coordinated restores the stop.
                    raise failure_type("rate_limited", status_code=429, retry_after=delay) from None
                raise AcceptanceStop(self.stopped) from None
            finally:
                self.local.sending = False

    def audit(self, event, args):
        if event.startswith(("socket.", "http.client.", "urllib.")):
            if not getattr(self.local, "sending", False) or self.reusing:
                self.stop("unexpected_network", event=event)
        elif event.startswith(("subprocess.", "os.spawn", "os.posix_spawn")) or event in {
            "os.system", "os.fork", "os.exec", "os.startfile",
        }:
            self.stop("subprocess_forbidden", event=event)

    def bind(self, tls, coordinator):
        self.transport = tls.HTTPSPost()
        self.transport_failure_type = tls.TushareHTTPSFailure
        tls.HTTPSPost = lambda: self
        original = coordinator.ProviderRequestCoordinator.execute_direct_physical_request
        original_workflow = coordinator.ProviderRequestCoordinator.execute_physical_workflow
        service, _ = coordinator.upstream_service_identity_for_provider("tushare")

        def execute(owner, **kwargs):
            return self.coordinated(original, owner, expected_service=service, **kwargs)

        coordinator.ProviderRequestCoordinator.execute_direct_physical_request = execute

        def execute_workflow(owner, **kwargs):
            return self.coordinated_workflow(original_workflow, owner, expected_service=service, **kwargs)

        coordinator.ProviderRequestCoordinator.execute_physical_workflow = execute_workflow


def validate_schema(connection, version):
    try:
        versions = tuple(row[0] for row in connection.execute(
            "SELECT version FROM schema_migrations ORDER BY version"))
    except Exception:
        raise AcceptanceStop("existing_schema_required") from None
    if versions != tuple(range(1, version + 1)):
        raise AcceptanceStop("schema_migration_required") from None


def require_imported_authority(connection, primary):
    source = os.path.normcase(str(primary.resolve()))
    try:
        row = connection.execute(
            "SELECT 1 FROM provider_request_state_imports WHERE source_database_path = ?",
            (source,),
        ).fetchone()
    except Exception:
        raise AcceptanceStop("existing_authority_required") from None
    if row is None:
        raise AcceptanceStop("coordinator_state_import_required") from None


def validate_calendar_page(guard, exchange, rows, membership):
    """Validate raw rows before provider deduplication, retaining cross-page dates."""
    window = guard.discovery
    for row in rows:
        try:
            value, flag = row["cal_date"], row["is_open"]
            if (row["exchange"] != exchange or type(value) is not str or len(value) != 8
                    or not value.isascii() or not value.isdigit()
                    or type(flag) not in (int, str) or str(flag) not in {"0", "1"}):
                raise ValueError
            day = datetime.strptime(value, "%Y%m%d").date()
            if not window.start <= day <= window.end or day in membership:
                raise ValueError
            membership[day] = str(flag) == "1"
        except (KeyError, TypeError, ValueError):
            guard.stop("calendar_response_invalid")


def discover_session(guard, fetch_calendar):
    """Select a scheduled session, never assert that its prices are published.

    The caller supplies the existing coordinated, HTTPS-guarded provider. Every
    page shares this guard's ledger with the later two-stock data phase.
    """
    if not guard.discovering:
        guard.stop("calendar_discovery_already_finished")
    window = guard.discovery
    expected = {window.start + timedelta(days=index) for index in range(31)}
    calendars = {}
    for exchange in ("SSE", "SZSE"):
        rows = fetch_calendar(exchange, window.start, window.end)
        membership = {}
        validate_calendar_page(guard, exchange, rows, membership)
        if set(membership) != expected:
            guard.stop("calendar_coverage_incomplete")
        calendars[exchange] = membership
    if calendars["SSE"] != calendars["SZSE"]:
        guard.stop("calendar_exchange_disagreement")
    common = [day for day in expected if calendars["SSE"][day]]
    if not common:
        guard.stop("calendar_no_common_open_session")
    selected = window.requested_end or max(common)
    if selected not in common:
        guard.stop("explicit_end_not_common_open_session")
    start = five_year_start(selected)
    try:
        guard.record({"kind": "calendar_selected", "selection": "explicit" if window.requested_end else "automatic",
            "run_date": window.run_date.isoformat(), "calendar_start": window.start.isoformat(),
            "calendar_end": window.end.isoformat(), "selected_end": selected.isoformat(),
            "start": start.isoformat(), "publication_checked": False,
            "calendars": {exchange: [[day.isoformat(), "open" if opened else "closed"]
                                      for day, opened in sorted(days.items())]
                          for exchange, days in calendars.items()}})
    except BaseException:
        guard.stop("calendar_ledger_failure")
    # One transition, after complete agreement and a retained selection record.
    # Neither the attempt count nor pacing/stop state is reset here.
    with guard.lock:
        guard.start, guard.end = start, selected
        guard.discovering = False
    return selected


def snapshot_facts(snapshot):
    """Compare provenance and normalized data, never credentials or model prose."""
    return {key: getattr(snapshot, key) for key in (
        "symbol", "provider", "frame_sha256", "snapshot_id", "retrieved_at",
        "adjustment_basis", "effective_trading_date", "pin_membership_digest",
    )}


def run_data_phases(guard, snapshot_module, record, *, end, require_closed=lambda: None):
    if guard.discovering or end != guard.end.isoformat():
        guard.stop("data_window_not_selected")
    first = {}
    for symbol in SYMBOLS:
        with snapshot_module.authoritative_snapshot_run():
            value = snapshot_module.get_authoritative_market_snapshot(
                symbol, guard.start.isoformat(), end, minimum_history_rows=200)
        require_closed()
        if value.provider != "tushare" or value.effective_trading_date != end:
            guard.stop("completed_session_or_provider_mismatch")
        first[symbol] = snapshot_facts(value)
        record({"kind": "initial", **first[symbol], "rows": len(value.frame),
                "history_store_status": value.history_store_status})
    before = guard.attempts
    with guard.cache_only():
        for symbol in SYMBOLS:
            # New request context; store methods close their connections on return.
            with snapshot_module.authoritative_snapshot_run():
                value = snapshot_module.get_authoritative_market_snapshot(
                    symbol, guard.start.isoformat(), end, minimum_history_rows=200)
            require_closed()
            if snapshot_facts(value) != first[symbol]:
                guard.stop("reopen_evidence_mismatch")
            record({"kind": "reopened", **snapshot_facts(value), "rows": len(value.frame),
                    "history_store_status": value.history_store_status})
    if guard.attempts != before:
        guard.stop("reuse_attempt_count_changed")
    record({"kind": "completed", "physical_attempts": guard.attempts,
            "reuse_attempts": 0, "model_calls": 0})


def closed_date(end, now=None):
    # Shanghai has a fixed UTC+8 offset. The market calendar still decides whether
    # the requested date is an actual session and the returned data are complete.
    current = now or datetime.now(timezone.utc)
    if end >= current.astimezone(timezone(timedelta(hours=8))).date():
        raise AcceptanceStop("choose_a_previous_completed_session") from None

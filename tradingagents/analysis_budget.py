"""Opt-in, process-exclusive analysis admission budget.

Counts transport attempts, not tokens, invoices, logical calls or coordinator
permits. The terminal exception deliberately bypasses ordinary provider/SDK
retry handlers. A stopped budget stays stopped, even if a caller catches it.
"""

from __future__ import annotations

import json
import math
import os
import sqlite3
import threading
import time
from asyncio import CancelledError
from contextlib import contextmanager, nullcontext, suppress
from contextvars import ContextVar, copy_context

_MODEL_REGENERATION_CONTRACT = "post200-incomplete-chunked-twice-v2"
_MODEL_REGENERATION_MAXIMUM = 2


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


def coerce_output_token_limit(value):
    """Explicit DeepSeek Chat Completions completion cap, never an invoice cap."""
    if value is None:
        return None
    if isinstance(value, str) and value.isascii() and value.isdecimal():
        value = int(value)
    if type(value) is not int or not 1 <= value <= 393216:
        raise ValueError("llm_max_output_tokens must be an integer from 1 to 393216")
    return value


def coerce_deepseek_reasoning_effort(value):
    """Explicit DeepSeek effort; None preserves the provider's default."""
    if value is not None and (type(value) is not str
            or value not in {"none", "low", "high", "max"}):
        raise ValueError("deepseek_reasoning_effort must be none, low, high or max")
    return value


class AnalysisBudget:
    def __init__(self, max_calls: int, timeout_seconds: float, *, clock=time.monotonic,
                 disable_alpha_vantage: bool = False, allowed_data_vendors=None,
                 checkpoint_required: bool = False, wall_clock=time.time,
                  max_output_tokens=None, skip_optional_fundamentals=False,
                   session_deadline_epoch=None, session_monotonic_deadline=None,
                   model_regeneration_enabled=False):
        if type(max_calls) is not int or max_calls < 1:
            raise ValueError("analysis_max_calls must be a positive integer")
        if not _valid_timeout(timeout_seconds):
            raise ValueError("analysis_timeout_seconds must be positive and finite")
        self.max_calls = max_calls
        self.max_output_tokens = coerce_output_token_limit(max_output_tokens)
        if type(skip_optional_fundamentals) is not bool:
            raise ValueError("analysis_skip_optional_fundamentals must be boolean")
        self.skip_optional_fundamentals = skip_optional_fundamentals
        if type(model_regeneration_enabled) is not bool:
            raise ValueError("analysis_model_regeneration_enabled must be boolean")
        self.model_regeneration_enabled = model_regeneration_enabled
        self._model_regenerations = {}
        self._model_regeneration_decisions = []
        self.timeout_seconds = timeout_seconds
        self._clock = clock
        self._started = clock()
        self._lock = threading.RLock()
        self._counts = {"model": 0, "data": 0}
        self._reason = None
        self._cache_hits = []
        self._finite_cache_reads = []
        self.disable_alpha_vantage = disable_alpha_vantage
        self._data_gaps = []
        self._data_transport_denial = None
        self.allowed_data_vendors = None if allowed_data_vendors is None else frozenset(allowed_data_vendors)
        self._checkpoint_required = checkpoint_required
        self._journal = None
        self._wall_clock = wall_clock
        if (session_deadline_epoch is None) != (session_monotonic_deadline is None):
            raise ValueError("shared session deadlines require both clock bounds")
        if session_deadline_epoch is not None and not all(
                _valid_timeout(v) for v in (session_deadline_epoch, session_monotonic_deadline)):
            raise ValueError("shared session deadlines must be positive and finite")
        self._session_deadline_epoch = session_deadline_epoch
        # Same-machine handoff preserves the original monotonic deadline. Never
        # re-anchor from the wall clock after process startup or phase changes.
        self._session_monotonic_deadline = session_monotonic_deadline
        self._deadline = None
        self._last_seen = None
        self._last_failure = None
        self._checkpoint_finished = False
        self._persisted = False
        self._fatal_reason = None
        self.completion_committed = False
        self._preparing = False
        self._preparation_owner = None
        self._analysis_entered = False

    @contextmanager
    def checkpoint_session(self, connection, connection_lock, thread, binding, *, has_checkpoint):
        try:
            self.bind_checkpoint(connection, connection_lock, thread, binding, has_checkpoint=has_checkpoint)
            yield
            if not self.completion_committed:
                self.check()
                self.complete()
        except BaseException:
            try:
                self.record_interruption()
            except AnalysisBudgetStopped:
                pass
            raise
        finally:
            self._journal = None
            self._checkpoint_finished = True

    def bind_checkpoint(self, connection, connection_lock, thread, binding, *, has_checkpoint):
        """Bind a durable admission ledger before any provider/model work.

        Uses the existing saver's connection and lock, independent of graph
        checkpoint commits. Reservations are committed BEFORE physical sends.
        Graph cleanup never deletes this table or its terminal tombstones.
        """
        if self._fatal_reason is not None:
            raise AnalysisBudgetStopped(self._fatal_reason)
        if self._persisted or self._journal is not None or self._counts != {"model": 0, "data": 0}:
            self.stop("checkpoint_budget_already_bound")
        if self._session_deadline_epoch is not None and self._remaining_without_journal() <= 0:
            self.stop("deadline_exceeded")
        policy_fields = {"binding": binding, "maximum": self.max_calls,
            "timeout": self.timeout_seconds, "disable_alpha": self.disable_alpha_vantage,
            "vendors": None if self.allowed_data_vendors is None else sorted(self.allowed_data_vendors)}
        if self.max_output_tokens is not None:
            policy_fields["max_output_tokens"] = self.max_output_tokens
        if self.skip_optional_fundamentals:
            policy_fields["skip_optional_fundamentals"] = True
        if self.model_regeneration_enabled:
            policy_fields["model_regeneration"] = _MODEL_REGENERATION_CONTRACT
        if self._session_deadline_epoch is not None:
            policy_fields["session_deadline_epoch"] = self._session_deadline_epoch
            policy_fields["session_monotonic_deadline"] = self._session_monotonic_deadline
        policy = json.dumps(policy_fields, sort_keys=True, separators=(",", ":"))
        with self._lock, connection_lock:
            try:
                connection.execute("PRAGMA synchronous=FULL")
                connection.execute("CREATE TABLE IF NOT EXISTS analysis_budget_v1 ("
                    "thread TEXT PRIMARY KEY, policy TEXT NOT NULL, state TEXT NOT NULL)")
                connection.commit()
                connection.execute("BEGIN IMMEDIATE")
                row = connection.execute("SELECT policy, state FROM analysis_budget_v1 WHERE thread=?", (thread,)).fetchone()
                if row is None:
                    if has_checkpoint:
                        raise AnalysisBudgetStopped("checkpoint_budget_missing")
                    now = self._wall_clock()
                    state = {"counts": dict(self._counts), "reason": None,
                              "deadline": now + (self.timeout_seconds if self._session_deadline_epoch is None
                                                  else self._remaining_without_journal(now)), "last_seen": now,
                             "last_failure": None}
                    connection.execute("INSERT INTO analysis_budget_v1 VALUES (?, ?, ?)",
                                       (thread, policy, json.dumps(state)))
                else:
                    if row[0] != policy:
                        raise AnalysisBudgetStopped("checkpoint_budget_mismatch")
                    state = json.loads(row[1])
                self._restore(state)
                # Never create/upgrade a recovery table for a mismatched old policy.
                if self.model_regeneration_enabled:
                    connection.execute("CREATE TABLE IF NOT EXISTS analysis_model_regeneration_v2 ("
                        "thread TEXT NOT NULL, logical_call TEXT NOT NULL, state TEXT NOT NULL, "
                        "PRIMARY KEY (thread, logical_call))")
                connection.commit()
            except BaseException:
                connection.rollback()
                raise
        self._journal = connection, connection_lock, thread, policy
        self._persisted = True
        self.check()

    def _restore(self, state):
        if (type(state) is not dict or set(state) != {"counts", "reason", "deadline", "last_seen", "last_failure"}
                or type(state["counts"]) is not dict or set(state["counts"]) != {"model", "data"}
                or any(type(n) is not int or n < 0 for n in state["counts"].values())
                or sum(state["counts"].values()) > self.max_calls
                or not _valid_timeout(state["deadline"]) or not _valid_timeout(state["last_seen"])
                or state["deadline"] < state["last_seen"]
                or state["reason"] is not None and not isinstance(state["reason"], str)
                or state["last_failure"] not in (None, "interrupted")):
            raise AnalysisBudgetStopped("checkpoint_budget_corrupt")
        self._counts = dict(state["counts"])
        self._reason = state["reason"]
        self._deadline = state["deadline"]
        self._last_seen = state["last_seen"]
        self._last_failure = state["last_failure"]

    def _durable(self, kind=None, reason=None, interrupted=False, *, optional=False):
        if self._fatal_reason is not None:
            raise AnalysisBudgetStopped(self._fatal_reason)
        connection, connection_lock, thread, policy = self._journal
        with connection_lock:
            try:
                connection.execute("BEGIN IMMEDIATE")
                row = connection.execute("SELECT policy, state FROM analysis_budget_v1 WHERE thread=?", (thread,)).fetchone()
                if row is None or row[0] != policy:
                    raise AnalysisBudgetStopped("checkpoint_budget_mismatch")
                self._restore(json.loads(row[1]))
                now = self._wall_clock()
                if not _valid_timeout(now) or now < self._last_seen:
                    self._reason = self._reason or "clock_rollback"
                remaining = min(self._deadline - now, self._remaining_without_journal(now))
                if remaining <= 0:
                    self._reason = self._reason or "deadline_exceeded"
                self._reason = self._reason or reason
                if kind is not None and self._reason is None:
                    if kind not in self._counts:
                        self._reason = "uncovered_transport"
                    elif sum(self._counts.values()) >= self.max_calls:
                        if optional:
                            remaining = None
                        else:
                            self._reason = "call_limit_exceeded"
                    else:
                        self._counts[kind] += 1
                if interrupted:
                    self._last_failure = "interrupted"
                self._last_seen = min(self._deadline, max(self._last_seen, now))
                state = {"counts": self._counts, "reason": self._reason,
                         "deadline": self._deadline, "last_seen": self._last_seen,
                         "last_failure": self._last_failure}
                connection.execute("UPDATE analysis_budget_v1 SET state=? WHERE thread=? AND policy=?",
                                   (json.dumps(state), thread, policy))
                connection.commit()
            except (sqlite3.Error, ValueError, TypeError, KeyError, AnalysisBudgetStopped) as exc:
                self._reason = "checkpoint_budget_persistence_failed"
                self._fatal_reason = self._reason
                try:
                    connection.rollback()
                except sqlite3.Error:
                    pass
                raise AnalysisBudgetStopped(self._reason) from exc
        if self._reason is not None:
            raise AnalysisBudgetStopped(self._reason)
        return remaining

    def record_interruption(self):
        if self._journal is not None:
            with self._lock:
                if self._fatal_reason is not None:
                    self._persist_fatal_only()
                    raise AnalysisBudgetStopped(self._fatal_reason)
                self._durable(interrupted=True)

    def claim_model_regeneration(self, logical_call_id):
        """Spend one of two session opportunities, at most once per logical call.

        This receipt is diagnostic, never authority for checkpoint recovery. The
        next physical send still reserves its own call against the SAME journal.
        """
        with self._lock:
            if not self.model_regeneration_enabled or self._journal is None:
                self.record_model_regeneration_decision("disabled_or_unbound", True)
                return False
            if self._counts["model"] < 1:
                return False
            if (type(logical_call_id) is not str or len(logical_call_id) > 32
                    or not logical_call_id.startswith("model-call-")
                    or not logical_call_id[11:].isascii() or not logical_call_id[11:].isdecimal()):
                return False
            try:
                self.check()
                if sum(self._counts.values()) >= self.max_calls:
                    self.stop("call_limit_exceeded")
            except AnalysisBudgetStopped:
                self.record_model_regeneration_decision("budget_stopped", True)
                raise
            connection, connection_lock, thread, _ = self._journal
            with connection_lock:
                try:
                    connection.execute("BEGIN IMMEDIATE")
                    legacy_table = connection.execute("SELECT 1 FROM sqlite_master WHERE type='table' "
                                                      "AND name='analysis_model_regeneration_v1'").fetchone()
                    if legacy_table and connection.execute(
                            "SELECT 1 FROM analysis_model_regeneration_v1 WHERE thread=?", (thread,)).fetchone():
                        connection.commit()
                        self.record_model_regeneration_decision("legacy_record_present", True)
                        return False
                    if connection.execute("SELECT 1 FROM analysis_model_regeneration_v2 WHERE thread=? AND logical_call=?",
                                          (thread, logical_call_id)).fetchone() is not None:
                        connection.commit()
                        self.record_model_regeneration_decision("logical_call_limit_reached", True)
                        return False
                    count = connection.execute("SELECT COUNT(*) FROM analysis_model_regeneration_v2 WHERE thread=?",
                                               (thread,)).fetchone()[0]
                    if count >= _MODEL_REGENERATION_MAXIMUM:
                        connection.commit()
                        self.record_model_regeneration_decision("session_limit_reached", True)
                        return False
                    record = {"contract": _MODEL_REGENERATION_CONTRACT,
                              "logical_call_id": logical_call_id, "first_stage": "response_body_read",
                              "first_http_status": 200, "first_error": "remote_protocol_incomplete_chunked",
                               "first_session_model_count": self._counts["model"],
                              "deadline": self._deadline, "outcome": "reserved",
                              "failed_request_usage": "unknown"}
                    connection.execute("INSERT INTO analysis_model_regeneration_v2 VALUES (?, ?, ?)",
                                       (thread, logical_call_id, json.dumps(record, sort_keys=True)))
                    connection.commit()
                    self._model_regenerations[logical_call_id] = record
                    self.record_model_regeneration_decision("admitted", True)
                    return True
                except sqlite3.Error:
                    self._fatal_reason = "model_regeneration_persistence_failed"
                    with suppress(sqlite3.Error):
                        connection.rollback()
                    raise AnalysisBudgetStopped(self._fatal_reason) from None

    def record_model_regeneration_decision(self, reason, response_closed):
        """Bounded closed diagnostics; never retain provider text or request data."""
        if reason not in {"disabled_or_unbound", "budget_stopped", "legacy_record_present",
                          "logical_call_limit_reached", "session_limit_reached", "admitted",
                          "non_target_failure", "response_close_failed", "context_ineligible"}:
            raise ValueError("invalid model regeneration decision")
        if type(response_closed) is not bool:
            raise ValueError("invalid model response close state")
        with self._lock:
            if len(self._model_regeneration_decisions) < 16:
                self._model_regeneration_decisions.append({"session_model_count": self._counts["model"],
                    "reason": reason, "response_closed": response_closed})

    def finish_model_regeneration(self, logical_call_id, outcome, *, preserve_error=False):
        """Close the bounded receipt without replacing an existing primary error."""
        if outcome not in {"response_complete", "failed", "stopped"}:
            raise ValueError("invalid model regeneration outcome")
        with self._lock:
            if logical_call_id not in self._model_regenerations or self._journal is None:
                raise AnalysisBudgetStopped("model_regeneration_unbound")
            connection, connection_lock, thread, _ = self._journal
            record = dict(self._model_regenerations[logical_call_id], outcome=outcome,
                          final_session_model_count=self._counts["model"])
            self._model_regenerations[logical_call_id] = record
            with connection_lock:
                try:
                    connection.execute("BEGIN IMMEDIATE")
                    cursor = connection.execute("UPDATE analysis_model_regeneration_v2 SET state=? WHERE thread=? AND logical_call=?",
                                                (json.dumps(record, sort_keys=True), thread, logical_call_id))
                    if cursor.rowcount != 1:
                        raise sqlite3.DatabaseError("missing recovery receipt")
                    connection.commit()
                except sqlite3.Error:
                    self._fatal_reason = "model_regeneration_persistence_failed"
                    with suppress(sqlite3.Error):
                        connection.rollback()
                    if not preserve_error:
                        raise AnalysisBudgetStopped(self._fatal_reason) from None

    def _persist_fatal_only(self):
        """One best-effort terminal write at teardown; never retries a send.

        If storage remains unavailable, only previously committed attempts are
        durable. The same object stays stopped regardless of this write.
        """
        connection, connection_lock, thread, policy = self._journal
        with connection_lock:
            try:
                connection.execute("BEGIN IMMEDIATE")
                row = connection.execute("SELECT policy, state FROM analysis_budget_v1 WHERE thread=?", (thread,)).fetchone()
                if row is None or row[0] != policy:
                    raise ValueError("unavailable budget row")
                state = json.loads(row[1])
                state["reason"] = state["reason"] or self._fatal_reason
                state["last_failure"] = "interrupted"
                connection.execute("UPDATE analysis_budget_v1 SET state=? WHERE thread=? AND policy=?",
                                   (json.dumps(state), thread, policy))
                connection.commit()
            except (sqlite3.Error, ValueError, TypeError, KeyError):
                try:
                    connection.rollback()
                except sqlite3.Error:
                    pass

    def complete(self):
        if self._journal is not None:
            with self._lock:
                try:
                    self._durable(reason="completed")
                except AnalysisBudgetStopped as exc:
                    if exc.reason != "completed":
                        raise
                    self.completion_committed = True

    def complete_before_cleanup(self, cleanup):
        """Keep a terminal tombstone before successful graph state is removed."""
        if not self.completion_committed:
            self.check()
            self.complete()
        return cleanup()

    def stop(self, reason: str):
        with self._lock:
            if self._journal is not None:
                self._durable(reason=reason)
            self._reason = self._reason or reason
            raise AnalysisBudgetStopped(self._reason)

    def _remaining_without_journal(self, now=None):
        remaining = self.timeout_seconds - (self._clock() - self._started)
        if self._session_deadline_epoch is not None:
            now = self._wall_clock() if now is None else now
            remaining = min(remaining, self._session_deadline_epoch - now,
                            self._session_monotonic_deadline - self._clock())
        return remaining

    def check(self) -> float:
        with self._lock:
            if self._journal is not None:
                return self._durable()
            if self._checkpoint_required:
                raise AnalysisBudgetStopped("checkpoint_budget_unbound")
            if self._reason is not None:
                raise AnalysisBudgetStopped(self._reason)
            if self._preparing:
                # Selection/construction is not the analysis deadline, and may
                # perform no physical business sends at all.
                return self.timeout_seconds
            remaining = self._remaining_without_journal()
            if remaining <= 0:
                self.stop("deadline_exceeded")
            return remaining

    def admit(self, kind: str, *, optional=False) -> float | None:
        remaining=self._admit_stage(kind, optional=optional)
        from tradingagents.full_session_ledger import owner,finance
        current=owner()
        if current is not None and remaining is not None:
            remaining=min(remaining,current.reserve("financial" if finance.get() else kind))
        return remaining

    def _admit_stage(self, kind, *, optional=False):
        with self._lock:
            if self._preparing:
                self.stop("preparation_network_forbidden")
            if self._journal is not None:
                return self._durable(kind=kind, optional=optional)
            remaining = self.check()
            if kind not in self._counts:
                self.stop("uncovered_transport")
            if sum(self._counts.values()) >= self.max_calls:
                if optional:
                    return None
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

    def record_finite_cache_read(self, envelope) -> None:
        with self._lock:
            self.check()
            item = {'contract':envelope.contract,'source_ref':envelope.source_ref,
                    'tool_call_id':envelope.tool_call_id,'new_physical_requests':0,
                    'read_at':envelope.read_at.isoformat(),
                    'original_observations':[x.model_dump(mode='json') for x in envelope.historical_observations]}
            if item not in self._finite_cache_reads:
                self._finite_cache_reads.append(item)

    def record_data_gap(self, tool: str, reason: str, *, provider="alpha_vantage") -> str:
        """Operational diagnostics only; never source facts or usable data."""
        with self._lock:
            self.check()
            item = {"tool": tool, "provider": provider, "reason": reason}
            if item not in self._data_gaps:
                self._data_gaps.append(item)
        return (
            f"DATA_UNAVAILABLE: {tool}; provider={provider}; reason={reason}. "
            "No cross-provider fallback. Report this gap; do not treat it as empty "
            "news, absence of transactions, or evidence for a factual claim."
        )

    def _record_data_redirect(self, request, status: int) -> None:
        """Record the already-admitted public endpoint, never query or Location.

        Called only after the core destination/auth guards accepted the request.
        This diagnostic is not a new admission rule or a checkpoint allowance.
        """
        from urllib.parse import urlsplit

        target = urlsplit(request.url)
        with self._lock:
            if self._data_transport_denial is None:
                self._data_transport_denial = {
                    "reason": "data_redirect_forbidden",
                    "method": request.method,
                    "host": target.hostname,
                    "path": target.path,
                    "http_status": status,
                }

    def receipt(self) -> dict:
        with self._lock:
            return {
                **({'finite_cache_reads':list(self._finite_cache_reads)} if self._finite_cache_reads else {}),
                "max_calls": self.max_calls,
                "max_output_tokens": self.max_output_tokens,
                "timeout_seconds": self.timeout_seconds,
                "admitted_attempts": sum(self._counts.values()),
                "model_attempts": self._counts["model"],
                "data_attempts": self._counts["data"],
                "stop_reason": self._reason,
                "last_failure": self._last_failure,
                "persistent": self._persisted,
                "financial_cache_hits": list(self._cache_hits),
                "data_gaps": [dict(item) for item in self._data_gaps],
                **({"data_transport_denial": dict(self._data_transport_denial)}
                   if self._data_transport_denial is not None else {}),
                **({"model_regeneration": {"contract": _MODEL_REGENERATION_CONTRACT,
                       "maximum": _MODEL_REGENERATION_MAXIMUM,
                       "attempts": [dict(v) for v in self._model_regenerations.values()]}}
                   if self._model_regenerations else {}),
                **({"model_regeneration_decisions": [dict(v) for v in self._model_regeneration_decisions]}
                   if self._model_regeneration_decisions else {}),
            }


_RUN_LOCK = threading.Lock()
_ACTIVE: AnalysisBudget | None = None
_UNBUDGETED_RUNS = 0
_IN_TRANSPORT: ContextVar[bool] = ContextVar("analysis_budget_transport", default=False)
_MODEL_REGENERATION: ContextVar[bool] = ContextVar("analysis_model_regeneration", default=False)


def active_budget() -> AnalysisBudget | None:
    # Process ownership also covers SDK-created threads which do not copy context.
    return _ACTIVE


def transport_is_admitted() -> bool:
    return _IN_TRANSPORT.get()


def model_regeneration_is_admitted() -> bool:
    return _IN_TRANSPORT.get() and _MODEL_REGENERATION.get()


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
        if (budget.allowed_data_vendors is not None and vendor not in budget.allowed_data_vendors):
            budget.stop("disabled_data_vendor")
        if vendor != "akshare":
            budget.stop("uncovered_data_vendor")


@contextmanager
def admitted_transport(kind: str, *, optional=False):
    budget = active_budget()
    if budget is None:
        yield None
        return
    remaining = budget.admit(kind, optional=True) if optional else budget.admit(kind)
    if remaining is None:
        yield None
        return
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
    import sys
    from contextlib import ExitStack
    from unittest.mock import patch

    import requests.adapters
    import requests.sessions

    original_requests = requests.adapters.HTTPAdapter.send
    original_session_request = requests.sessions.Session.request
    original_prepare_request = requests.sessions.Session.prepare_request
    original_sleep = time.sleep
    original_async_sleep = asyncio.sleep
    original_executor = asyncio.BaseEventLoop.run_in_executor
    original_debug_resolver = asyncio.BaseEventLoop._getaddrinfo_debug
    # Scheduler wakeups are not provider traffic. Track each real loop's own
    # pair by object identity, including loops created after the guard starts.
    internal_pairs = []
    making_self_pipe = ContextVar("analysis_budget_self_pipe", default=None)
    fallback_pair = getattr(socket, "_fallback_socketpair", None)
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        pass
    else:
        internal_pairs.append((loop, getattr(loop, "_ssock", None),
                               getattr(loop, "_csock", None)))

    def internal_socket(sock):
        return any(
            sock is saved and sock is getattr(owner, attribute, None)
            and sock.fileno() >= 0
            for owner, reader, writer in internal_pairs
            for saved, attribute in ((reader, "_ssock"), (writer, "_csock"))
        )

    def make_self_pipe(original):
        def create(loop):
            state = {"loop": loop, "connect_used": False}
            token = making_self_pipe.set(state)
            try:
                original(loop)
                reader, writer = loop._ssock, loop._csock
                # CPython's socketpair authenticates the peers. Do not register
                # descriptors, arbitrary localhost sockets, or replacement pairs.
                if (type(reader) is not socket.socket or type(writer) is not socket.socket
                        or reader.getsockname() != writer.getpeername()
                        or writer.getsockname() != reader.getpeername()):
                    _ACTIVE.stop("uncovered_scheduler_transport")
                internal_pairs.append((loop, reader, writer))
            finally:
                making_self_pipe.reset(token)
        return create

    def scheduler_pair_connect(sock, args, kwargs, caller):
        state = making_self_pipe.get()
        if (state is None or state["connect_used"] or kwargs or len(args) != 1
                or fallback_pair is None or caller.f_code is not fallback_pair.__code__
                or caller.f_locals.get("csock") is not sock):
            return False
        listener = caller.f_locals.get("lsock")
        if type(listener) is not socket.socket or type(sock) is not socket.socket:
            return False
        address = listener.getsockname()
        if (sock.family not in (socket.AF_INET, socket.AF_INET6)
                or listener.family != sock.family or sock.type != socket.SOCK_STREAM
                or listener.type != socket.SOCK_STREAM or sock.proto != 0
                or address[0] not in ("127.0.0.1", "::1")
                or address[1] <= 0 or args[0] != address[:2]):
            return False
        state["connect_used"] = True
        return True

    def bounded_sleep(delay):
        original_sleep(min(delay, _ACTIVE.check()))
        _ACTIVE.check()

    async def bounded_async_sleep(delay, result=None):
        value = await original_async_sleep(min(delay, _ACTIVE.check()), result=result)
        _ACTIVE.check()
        return value

    def session_request(session, *args, **kwargs):
        from tradingagents.budget_data import core_data_policy

        if not core_data_policy():
            return original_session_request(session, *args, **kwargs)
        prior = session.trust_env
        session.trust_env = False  # No netrc, environment proxy or implicit CA override.
        try:
            return original_session_request(session, *args, **kwargs)
        finally:
            session.trust_env = prior

    def prepare_request(session, request):
        from tradingagents.budget_data import check_core_request_auth, core_data_policy

        if not core_data_policy():
            return original_prepare_request(session, request)
        check_core_request_auth(request, session)
        prior = session.trust_env
        session.trust_env = False
        try:
            return original_prepare_request(session, request)
        finally:
            session.trust_env = prior

    def requests_send(adapter, request, **kwargs):
        from tradingagents.budget_data import check_budget_data_destination, core_data_policy

        check_budget_data_destination(request)
        core = core_data_policy()
        if core and (kwargs.get("proxies") or kwargs.get("verify", True) is not True
                     or kwargs.get("cert") is not None):
            _ACTIVE.stop("uncovered_data_transport_settings")
        if (type(adapter) is not requests.adapters.HTTPAdapter
                or adapter.max_retries.total not in (0, False)):
            _ACTIVE.stop("uncovered_requests_adapter")
        from tradingagents.full_notice import matches, OptionalNoticeSkipped
        from tradingagents.full_lpr import matches as matches_lpr
        with admitted_transport("data", optional=matches(request) or matches_lpr(request)) as remaining:
            if remaining is None:
                raise OptionalNoticeSkipped()
            kwargs["timeout"] = _timeout(kwargs.get("timeout"), remaining)
            response = original_requests(adapter, request, **kwargs)
            if core and 300 <= response.status_code < 400:
                _ACTIVE._record_data_redirect(request, response.status_code)
                response.close()
                _ACTIVE.stop("data_redirect_forbidden")
            _ = response.content
            return response

    def check_httpx(client, request, expected):
        if getattr(client, "_tradingagents_budget_provider", None) != "deepseek":
            _ACTIVE.stop("uncovered_model_transport")
        transport = client._transport_for_url(request.url)
        if type(transport) is not expected or transport._pool._retries != 0:
            _ACTIVE.stop("uncovered_httpx_transport")
        from tradingagents.budget_data import core_data_policy

        if core_data_policy() or _ACTIVE.max_output_tokens is not None:
            pool = transport._pool
            context = getattr(pool, "_ssl_context", None)
            if (getattr(pool, "_proxy_url", None) is not None
                    or getattr(pool, "_uds", None) is not None
                    or getattr(context, "verify_mode", None) != ssl.CERT_REQUIRED
                    or getattr(context, "check_hostname", None) is not True):
                _ACTIVE.stop("uncovered_model_transport_settings")
            target = request.url
            if (target.scheme != "https" or target.host != "api.deepseek.com"
                    or target.port not in (None, 443) or target.username or target.password
                    or target.path not in {"/chat/completions", "/v1/chat/completions"}
                    or target.query or target.fragment or request.method != "POST"):
                _ACTIVE.stop("uncovered_model_destination")
        if _ACTIVE.max_output_tokens is not None:
            try:
                body = json.loads(request.content)
                value = body.get("max_tokens")
                valid = (type(value) is int and 1 <= value <= _ACTIVE.max_output_tokens
                         and "max_completion_tokens" not in body)
            except (ValueError, TypeError, AttributeError):
                valid = False
            if not valid:
                _ACTIVE.stop("model_output_limit_unbound")

    def model_redirect_forbidden(response):
        from tradingagents.budget_data import core_data_policy

        return ((core_data_policy() or _ACTIVE.max_output_tokens is not None)
                and 300 <= response.status_code < 400)

    def set_httpx_timeout(request, remaining):
        old = request.extensions.get("timeout", {})
        request.extensions["timeout"] = {
            key: _timeout(old.get(key), remaining)
            for key in ("connect", "read", "write", "pool")
        }
        # Internal numeric observation only; never a wire header or a new check/send.
        request.extensions["tradingagents.admitted_deadline"] = (
            remaining, _ACTIVE._deadline or _ACTIVE._session_deadline_epoch)

    def httpx_sends(module):
        # Bind each family separately; a hook must never call the other
        # family's original method or accept its transport types by accident.
        original_sync = module.Client._send_single_request
        original_async = module.AsyncClient._send_single_request

        def sync_send(client, request):
            from tradingagents.llm_clients.openai_client import (
                _ACTIVE_MODEL_TRANSPORT_CALL,
                _RESPONSE_METADATA_EXTENSION,
                _capture_response_metadata,
                _record_sync_transport_send,
            )

            check_httpx(client, request, module.HTTPTransport)
            budget = _ACTIVE
            context = _ACTIVE_MODEL_TRANSPORT_CALL.get()
            eligible_error = None
            read_failure = None

            def once():
                nonlocal eligible_error, read_failure
                eligible_error = None
                read_failure = None
                # Response metadata belongs to this attempt, never a prior 200.
                request.extensions.pop(_RESPONSE_METADATA_EXTENSION, None)
                with admitted_transport("model") as remaining:
                    set_httpx_timeout(request, remaining)

                    def send_and_read(req):
                        nonlocal eligible_error, read_failure
                        response = original_sync(client, req)
                        _capture_response_metadata(response)
                        if model_redirect_forbidden(response):
                            try:
                                response.close()
                            finally:
                                budget.stop("model_redirect_forbidden")
                        try:
                            response.read()
                        except BaseException as error:
                            closed = False
                            try:
                                response.close()
                                closed = response.is_closed is True
                            except BaseException:
                                pass  # Preserve the read failure; no recovery after cleanup failure.
                            read_failure = ("non_target_failure" if closed else "response_close_failed", closed)
                            if (closed and response.status_code == 200
                                    and type(error) is module.RemoteProtocolError
                                    and error.args == ("peer closed connection without sending complete message body (incomplete chunked read)",)):
                                eligible_error = error
                            raise
                        return response

                    return _record_sync_transport_send(send_and_read, request)

            try:
                return once()
            except Exception as error:
                # Only the admitted, synchronous, zero-SDK-retry DeepSeek path.
                # The same immutable request bytes are re-sent, not the graph node.
                if error is not eligible_error:
                    if read_failure is not None and budget.model_regeneration_enabled:
                        budget.record_model_regeneration_decision(*read_failure)
                    raise
                if (context is None or budget is not _ACTIVE
                        or context.provider_family != "deepseek" or context.configured_max_retries != 0
                        or type(request.stream) is not module.ByteStream):
                    budget.record_model_regeneration_decision("context_ineligible", True)
                    raise
                if not budget.claim_model_regeneration(context.logical_call_id):
                    raise
                marker = _MODEL_REGENERATION.set(True)
                try:
                    try:
                        result = once()
                    except BaseException as final_error:
                        if read_failure is not None:
                            budget.record_model_regeneration_decision(
                                "logical_call_limit_reached" if final_error is eligible_error else read_failure[0],
                                read_failure[1])
                        budget.finish_model_regeneration(
                            context.logical_call_id,
                            "stopped" if isinstance(final_error, (AnalysisBudgetStopped, KeyboardInterrupt, SystemExit)) else "failed",
                            preserve_error=True)
                        raise
                finally:
                    _MODEL_REGENERATION.reset(marker)
                budget.finish_model_regeneration(context.logical_call_id, "response_complete")
                return result

        async def async_send(client, request):
            from tradingagents.llm_clients.openai_client import (
                _capture_response_metadata,
                _record_async_transport_send,
            )

            check_httpx(client, request, module.AsyncHTTPTransport)
            with admitted_transport("model") as remaining:
                set_httpx_timeout(request, remaining)

                async def send_and_read(req):
                    response = await original_async(client, req)
                    _capture_response_metadata(response)
                    try:
                        if model_redirect_forbidden(response):
                            _ACTIVE.stop("model_redirect_forbidden")
                        await response.aread()
                    except BaseException as primary_error:
                        try:
                            await response.aclose()
                        except BaseException as cleanup_error:
                            terminal = (AnalysisBudgetStopped, CancelledError, KeyboardInterrupt, SystemExit)
                            # Ordinary cleanup errors never replace the first error.
                            # A new cancellation/stop still propagates, retaining the
                            # original read failure as its cause; no async regeneration.
                            if isinstance(cleanup_error, terminal) and not isinstance(primary_error, terminal):
                                raise cleanup_error from primary_error
                        raise
                    return response

                return await _record_async_transport_send(send_and_read, request)

        return sync_send, async_send

    def guarded_socket(original):
        def call(sock, *args, **kwargs):
            if internal_socket(sock):
                return original(sock, *args, **kwargs)
            # Windows implements socketpair with a temporary local listener.
            # Only the captured stdlib creator's exact client connect, while
            # making a loop's self-pipe, is exempt. Direct socketpair calls and
            # all unrelated local/external connects retain normal admission.
            if original.__name__ == "connect" and scheduler_pair_connect(
                    sock, args, kwargs, sys._getframe(1)):
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

    def guarded_resolver(original):
        def call(*args, **kwargs):
            if _ACTIVE is None:
                raise AnalysisBudgetStopped("inactive_transport")
            if not _IN_TRANSPORT.get():
                _ACTIVE.stop("uncovered_socket_transport")
            _ACTIVE.check()
            return original(*args, **kwargs)
        return call

    def resolver_executor(loop, executor, function, *args):
        # asyncio delegates getaddrinfo to a worker without ContextVar copying.
        # Carry the existing admission only for its exact resolver callbacks;
        # ordinary executor jobs do not acquire transport authority.
        resolver = function is socket.getaddrinfo or (
            getattr(function, "__self__", None) is loop
            and getattr(function, "__func__", None) is original_debug_resolver)
        if resolver and _IN_TRANSPORT.get():
            return original_executor(loop, executor, copy_context().run, function, *args)
        return original_executor(loop, executor, function, *args)

    with ExitStack() as stack:
        from asyncio.selector_events import BaseSelectorEventLoop

        loop_classes = [BaseSelectorEventLoop]
        if sys.platform == "win32":
            from asyncio.proactor_events import BaseProactorEventLoop

            loop_classes.append(BaseProactorEventLoop)
        for loop_class in loop_classes:
            stack.enter_context(patch.object(
                loop_class, "_make_self_pipe", make_self_pipe(loop_class._make_self_pipe)))
        stack.enter_context(patch.object(requests.sessions.Session, "request", session_request))
        stack.enter_context(patch.object(requests.sessions.Session, "prepare_request", prepare_request))
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
        for name in ("getaddrinfo", "gethostbyname", "gethostbyname_ex", "gethostbyaddr"):
            stack.enter_context(patch.object(socket, name, guarded_resolver(getattr(socket, name))))
        stack.enter_context(patch.object(asyncio.BaseEventLoop, "run_in_executor", resolver_executor))
        # Yahoo/curl uses native I/O, outside Python's socket boundary.
        try:
            import curl_cffi
        except ImportError:
            pass
        else:
            stack.enter_context(patch.object(curl_cffi.Curl, "perform", reject_native))
            stack.enter_context(patch.object(curl_cffi.AsyncCurl, "add_handle", reject_native))
        yield


def checkpoint_storage_directory(config: dict) -> str:
    """Select checkpoint storage without relocating financial/market caches.

    None preserves the legacy location. Explicit locations must be absolute,
    local paths; normalization is lexical and performs no filesystem access.
    Choosing an empty location starts a new run, not a resume of another one.
    """
    value = config.get("checkpoint_data_dir")
    if value is None:
        return config["data_cache_dir"]
    if (not isinstance(value, str) or not value or value != value.strip()
            or any(ord(character) < 32 for character in value)):
        raise ValueError("checkpoint_data_dir:must_be_absolute_local_path")
    normalized = os.path.normcase(os.path.normpath(value))
    if normalized.startswith(("\\\\", "//")) or not os.path.isabs(normalized):
        raise ValueError("checkpoint_data_dir:must_be_absolute_local_path")
    if os.name == "nt":
        drive, tail = os.path.splitdrive(normalized)
        if (len(drive) != 2 or drive[0] not in "abcdefghijklmnopqrstuvwxyz" or drive[1] != ":"
                or not tail.startswith("\\") or ":" in tail):
            raise ValueError("checkpoint_data_dir:must_be_absolute_local_path")
    return normalized


def inspect_budget_config(config: dict) -> tuple[tuple[str, str], ...]:
    """Collect fixed-safe configuration issues without constructing a budget."""
    issues = []
    if type(config.get("analysis_skip_optional_fundamentals", False)) is not bool:
        issues.append(("analysis_skip_optional_fundamentals", "must_be_boolean"))
    elif config.get("analysis_skip_optional_fundamentals", False) and not config.get("analysis_budget_enabled", False):
        issues.append(("analysis_skip_optional_fundamentals", "requires_analysis_budget"))
    if config.get("checkpoint_data_dir") is not None:
        try:
            checkpoint_storage_directory(config)
        except ValueError:
            issues.append(("checkpoint_data_dir", "must_be_absolute_local_path"))
    try:
        limit = coerce_output_token_limit(config.get("llm_max_output_tokens"))
        if limit is not None and str(config.get("llm_provider", "")).lower() != "deepseek":
            issues.append(("llm_max_output_tokens", "requires_deepseek"))
    except ValueError:
        issues.append(("llm_max_output_tokens", "must_be_positive_supported_integer"))
    try:
        effort = coerce_deepseek_reasoning_effort(config.get("deepseek_reasoning_effort"))
        if effort is not None and str(config.get("llm_provider", "")).lower() != "deepseek":
            issues.append(("deepseek_reasoning_effort", "requires_deepseek"))
    except ValueError:
        issues.append(("deepseek_reasoning_effort", "invalid_explicit_effort"))
    vendors = config.get("analysis_allowed_data_vendors")
    if vendors is not None and (not isinstance(vendors, (list, tuple)) or not vendors
            or any(type(v) is not str or v not in {"akshare", "tushare", "alpha_vantage", "fred", "polymarket"} for v in vendors)
            or len(set(vendors)) != len(vendors)):
        issues.append(("analysis_allowed_data_vendors", "invalid_explicit_policy"))
    if vendors is not None and config.get("analysis_budget_enabled") is not True:
        issues.append(("analysis_allowed_data_vendors", "requires_analysis_budget"))
    enabled = config.get("analysis_budget_enabled", False)
    regeneration = config.get("analysis_model_regeneration_enabled", False)
    if type(regeneration) is not bool:
        issues.append(("analysis_model_regeneration_enabled", "must_be_boolean"))
    elif regeneration and (enabled is not True or config.get("checkpoint_enabled") is not True
                           or config.get("llm_max_retries") != 0):
        issues.append(("analysis_model_regeneration_enabled", "requires_persistent_budget_and_zero_sdk_retries"))
    cache_only = config.get("financial_cache_only", False)
    if type(config.get("checkpoint_enabled", False)) is not bool:
        issues.append(("checkpoint_enabled", "must_be_boolean"))
    if cache_only is True and config.get("checkpoint_enabled") is True and enabled is not True:
        issues.append(("checkpoint_enabled", "cache_only_resume_requires_analysis_budget"))
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
                               ("mainland_capability_routing_mode", "legacy")):
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
    deadline = config.get("analysis_session_deadline_epoch")
    monotonic_deadline = config.get("analysis_session_monotonic_deadline")
    if deadline is not None or monotonic_deadline is not None:
        if enabled is not True:
            issues.append(("analysis_session_deadline_epoch", "requires_analysis_budget"))
        for field, value in (("analysis_session_deadline_epoch", deadline),
                             ("analysis_session_monotonic_deadline", monotonic_deadline)):
            if not _valid_timeout(value):
                issues.append((field, "requires_paired_positive_finite_clock_bounds"))
    return tuple(issues)


def validate_budget_config(config: dict) -> None:
    issues = inspect_budget_config(config)
    if issues:
        raise ValueError("analysis_budget:invalid_config; " + "; ".join(
            field + ":" + reason for field, reason in issues
        ))


def checkpoint_budget_binding(config, graph_signature):
    """Non-secret execution policy; changing it cannot mint a new budget."""
    from hashlib import sha256

    keys = ("llm_provider", "backend_url", "quick_think_llm", "deep_think_llm",
            "llm_max_retries", "financial_cache_only", "data_cache_dir",
            "market_history_database_path", "market_history_payload_root",
            "data_usage_mode", "market_history_mode", "mainland_capability_routing_mode",
            "tushare_account_scope_label", "tushare_financial_https_endpoint",
            "tushare_financial_cache_max_age_seconds", "tushare_financial_force_refresh",
            "budget_require_macro_sources", "tool_vendors", "data_vendors", "market_data_vendors")
    payload = {"contract": "checkpoint-budget-v1", "graph": graph_signature,
               "policy": {key: config.get(key) for key in keys}}
    calendar_digest = config.get("analysis_calendar_receipt_sha256")
    if calendar_digest is not None:
        count = config.get("analysis_calendar_calls")
        if (type(calendar_digest) is not str or len(calendar_digest) != 64
                or any(character not in "0123456789abcdef" for character in calendar_digest)
                or type(count) is not int or count not in (0, 1)):
            raise ValueError("analysis_budget:invalid_calendar_handoff")
        payload["policy"]["calendar_receipt_sha256"] = calendar_digest
        payload["policy"]["calendar_calls"] = count
    if config.get("checkpoint_data_dir") is not None:
        payload["policy"]["checkpoint_data_dir"] = checkpoint_storage_directory(config)
    if config.get("llm_max_output_tokens") is not None:
        payload["policy"]["llm_max_output_tokens"] = coerce_output_token_limit(config["llm_max_output_tokens"])
    if config.get("deepseek_reasoning_effort") is not None:
        payload["policy"]["deepseek_reasoning_effort"] = coerce_deepseek_reasoning_effort(config["deepseek_reasoning_effort"])
    if config.get("analysis_skip_optional_fundamentals", False):
        payload["policy"]["analysis_skip_optional_fundamentals"] = True
    if config.get("analysis_model_regeneration_enabled", False):
        payload["policy"]["model_regeneration"] = _MODEL_REGENERATION_CONTRACT
    moneyflow = config.get("bounded_moneyflow_enabled", False)
    if type(moneyflow) is not bool:
        raise ValueError("analysis_budget:invalid_moneyflow_option")
    if moneyflow:
        payload["policy"]["bounded_moneyflow"] = "bounded-moneyflow-v4"
    cache_mode = config.get("bounded_observation_cache_mode", "disabled")
    if cache_mode not in ("disabled", "retrospective", "as_known_on_date"):
        raise ValueError("analysis_budget:invalid_observation_cache_mode")
    if cache_mode != "disabled":
        payload["policy"]["bounded_observation_cache"] = cache_mode + "-v1"
    pin = config.get("bounded_fundflow_observation_sha256")
    if pin is not None:
        if not isinstance(pin, str) or len(pin) != 64 or any(c not in "0123456789abcdef" for c in pin):
            raise ValueError("analysis_budget:invalid_fundflow_observation_pin")
        payload["policy"]["bounded_fundflow_observation_sha256"] = pin
    margin_date = config.get("bounded_margin_date")
    if margin_date is not None:
        from datetime import date

        if type(margin_date) is not str or date.fromisoformat(margin_date).isoformat() != margin_date:
            raise ValueError("analysis_budget:invalid_margin_date")
        payload["policy"]["bounded_margin_balance"] = {"contract": "bounded-margin-balance-v2", "trade_date": margin_date}
    return sha256(json.dumps(payload, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()).hexdigest()


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
    preparation = None
    with _RUN_LOCK:
        if _ACTIVE is not None and _ACTIVE._preparing:
            preparation = _ACTIVE
            if (preparation._preparation_owner != threading.get_ident()
                    or preparation._analysis_entered):
                raise AnalysisBudgetStopped("concurrent_analysis_unsupported")
            preparation.check()  # A swallowed preparation rejection remains terminal.
        if (_ACTIVE is not None and preparation is None) or _UNBUDGETED_RUNS:
            raise AnalysisBudgetStopped("concurrent_analysis_unsupported")
        budget = AnalysisBudget(config.get("analysis_max_calls", 30),
                                config.get("analysis_timeout_seconds", 600),
                                disable_alpha_vantage=config.get("analysis_disable_alpha_vantage", False),
                                allowed_data_vendors=config.get("analysis_allowed_data_vendors"),
                                checkpoint_required=config.get("checkpoint_enabled", False),
                                max_output_tokens=config.get("llm_max_output_tokens"),
                                skip_optional_fundamentals=config.get("analysis_skip_optional_fundamentals", False),
                                session_deadline_epoch=config.get("analysis_session_deadline_epoch"),
                                session_monotonic_deadline=config.get("analysis_session_monotonic_deadline"),
                                model_regeneration_enabled=config.get("analysis_model_regeneration_enabled", False))
        _ACTIVE = budget
        if preparation is not None:
            preparation._analysis_entered = True
    try:
        # The preparation owner already holds the exact same process hooks.
        # Reinstalling here would count one physical send twice.
        with nullcontext() if preparation is not None else _transport_guards():
            yield budget
            if not budget._checkpoint_required:
                budget.check()
            elif not budget._checkpoint_finished:
                budget.stop("checkpoint_budget_unbound")
    except BaseException:
        if preparation is not None and budget._reason is not None:
            preparation._reason = budget._reason
        try:
            budget.record_interruption()
        except AnalysisBudgetStopped:
            pass  # Preserve the original failure, never refund its reservation.
        raise
    finally:
        with _RUN_LOCK:
            _ACTIVE = preparation


@contextmanager
def analysis_preparation_scope(config: dict):
    """For the CLI selection/construction phase: no business network permitted.

    The same owning thread may enter one analysis_budget_scope after final
    configuration validation. Its durable budget still starts at the original
    analysis boundary; preparation does not grant or consume a second quota.
    Module imports before this public CLI call are outside this scope.
    """
    global _ACTIVE
    if not config.get("analysis_budget_enabled", False):
        yield
        return
    preparation = AnalysisBudget(1, 600)
    preparation._preparing = True
    preparation._preparation_owner = threading.get_ident()
    with _RUN_LOCK:
        if _ACTIVE is not None or _UNBUDGETED_RUNS:
            raise AnalysisBudgetStopped("concurrent_analysis_unsupported")
        _ACTIVE = preparation
    try:
        with _transport_guards():
            yield
            preparation.check()
    finally:
        with _RUN_LOCK:
            _ACTIVE = None



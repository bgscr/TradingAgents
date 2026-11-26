"""Candidate bounded-entry adapters. Not installed or authorized for live execution.

Credential input remains owned by the entry. No schema migration, provider switch,
automatic retry, price acquisition or source/pin update is performed here.
"""
from __future__ import annotations

import json
import math
import os
import sqlite3
import time
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from hashlib import sha256
from pathlib import Path

from . import calendar_preparation as policy


def encoded(value):
    return json.dumps(value, sort_keys=True, ensure_ascii=False, allow_nan=False,
                      separators=(",", ":")).encode("utf-8")


def paced_wait(seconds, *, gate, clock, sleep=time.sleep, monotonic=time.monotonic):
    policy.require(type(seconds) in (int, float) and math.isfinite(seconds) and seconds >= 0,
                   'calendar_pacing_invalid')
    end = monotonic() + seconds
    while True:
        allowance = min(gate.check(), clock.remaining())
        remaining = end - monotonic()
        if remaining <= 0:
            return
        sleep(min(remaining, allowance))


def cleanup_failure(primary, cleanup, gate):
    return policy.CalendarStopped('calendar_workflow_cleanup_failed', details={
        'primary_code': gate.fatal or 'calendar_workflow_failed',
        'primary_type': type(primary).__name__, 'cleanup_code': 'calendar_queue_cancel_failed',
        'cleanup_type': type(cleanup).__name__, 'cleanup_confirmed': False})


def regular(path, *, boundary, required=True):
    policy.require(Path(path).is_absolute() and '..' not in Path(path).parts,
                   "calendar_unsafe_path")
    path = Path(path).absolute()
    boundary = Path(boundary).absolute()
    policy.require(path.is_relative_to(boundary), "calendar_unsafe_path")
    item = path
    while True:
        try:
            info = item.lstat()
        except FileNotFoundError:
            pass
        else:
            policy.require(not item.is_symlink() and not getattr(info, "st_file_attributes", 0) & 0x400,
                           "calendar_unsafe_path")
        if item == boundary:
            break
        item = item.parent
    policy.require(not required or path.is_file(), "calendar_missing_file")
    return path


def new_json(path, value, *, boundary):
    path = regular(path, boundary=boundary, required=False)
    payload = encoded(value)
    with path.open("xb") as handle:
        handle.write(payload)
        handle.flush()
        os.fsync(handle.fileno())
    return sha256(payload).hexdigest()


def read_json(path, *, boundary):
    path = regular(path, boundary=boundary)
    policy.require(path.stat().st_size <= 65536, "calendar_receipt_invalid")
    raw = path.read_bytes()
    value = json.loads(raw, object_pairs_hook=policy.unique_object)
    return value, sha256(raw).hexdigest()


def checked_settings(config, source_root):
    from .config import MarketHistoryConfig
    for name in ('database_path', 'payload_root', 'backup_root'):
        regular(config['market_history_' + name], boundary=source_root, required=False)
    return MarketHistoryConfig.from_mapping(config)


def expected_context(config, *, root, source_root, symbol, identity_revision, venue, as_of, szse_refresh=False):
    from .config import provider_request_authority_config
    from .current_cache import prepared_exchange_calendar_provider
    settings = checked_settings(config, source_root)
    authority = provider_request_authority_config(settings)
    paths = {}
    for name, path in (("market", settings.database_path), ("coordinator", authority.database_path)):
        path = regular(path, boundary=source_root)
        info = path.stat()
        paths[name] = {"path": str(path), "file_id": [info.st_dev, info.st_ino]}
    payload = regular(settings.payload_root, boundary=source_root, required=False)
    policy.require(payload.is_dir(), "calendar_payload_owner_mismatch")
    info = payload.stat()
    paths["payloads"] = {"path": str(payload), "file_id": [info.st_dev, info.st_ino]}
    cap = config.get("session_max_calls", 60)
    policy.require(type(cap) is int and 1 <= cap <= 60, "invalid_session_max_calls")
    identity_calls = config.get("session_identity_calls", 0)
    policy.require(type(identity_calls) is int and identity_calls in (0, 1), "invalid_identity_count")
    return {**({"session_identity_calls": identity_calls} if identity_calls else {}), **({"session_max_calls": cap} if cap != 60 else {}), "root": str(Path(root).absolute()), "session": Path(root).name,
            "source_root": str(Path(source_root).absolute()),
            "symbol": symbol, "identity_revision": identity_revision, "venue": venue,
            "analysis_date": as_of, "paths": paths,
            "refresh_source": prepared_exchange_calendar_provider({"XSHG":"SSE","XSHE":"SZSE"}[venue]).provider_dataset_id if venue=="XSHG" or (venue=="XSHE" and szse_refresh) else None,
            "destination": policy.DESTINATION if venue=="XSHG" or (venue=="XSHE" and szse_refresh) else None}


class RunLedger:
    """A new run owns one exclusive admission file; partial/unknown writes consume it."""
    def __init__(self, root, binding, context):
        self.root, self.binding, self.context = Path(root).absolute(), tuple(binding), context

    def admit_calendar_once(self, binding):
        policy.require(tuple(binding) == self.binding, "calendar_admission_binding_mismatch")
        try:
            new_json(self.root / "calendar-admission.json", {
                "contract": policy.CONTRACT, "binding": list(self.binding),
                "calendar_calls": 1, "combined_max_calls": self.context.get("session_max_calls", 60), "context": self.context,
            }, boundary=self.root)
        except FileExistsError:
            raise policy.CalendarStopped("calendar_request_limit") from None

    def complete(self, result):
        path = self.root / "calendar-admission.json"
        if result["physical_attempts"] == 0:
            policy.require(not path.exists() and result["action"] == "reused",
                           "calendar_admission_outcome_unknown")
            admission_digest = None
            # Another owner can publish after readiness and before this recheck.
            # Keep every zero-send XSHE receipt compatible with the prior format.
            if self.context['venue']=='XSHE':
                self.context={**self.context,'refresh_source':None,'destination':None}
        else:
            admission, admission_digest = read_json(path, boundary=self.root)
            policy.require(admission == {"contract": policy.CONTRACT, "binding": list(self.binding),
                "calendar_calls": 1, "combined_max_calls": self.context.get("session_max_calls", 60), "context": self.context}, "calendar_receipt_invalid")
        receipt = {"contract": policy.CONTRACT, "binding": list(self.binding),
                   "status": "ready", "result": result,
                   "calendar_calls": result["physical_attempts"],
                   "admission_sha256": admission_digest,
                   "analysis_max_calls": self.context.get("session_max_calls", 60) - self.context.get("session_identity_calls", 0) - result["physical_attempts"]}
        receipt["context"] = self.context
        digest = new_json(self.root / "calendar-handoff.json", receipt, boundary=self.root)
        return {"binding": list(self.binding), "sha256": digest,
                "calendar_calls": receipt["calendar_calls"], "analysis_max_calls": receipt["analysis_max_calls"],
                "context": self.context}


def verify_handoff(root, binding, context):
    value, digest = read_json(Path(root) / "calendar-handoff.json", boundary=root)
    policy.require(type(binding) is dict and digest == binding.get("sha256")
                   and value.get("contract") == policy.CONTRACT
                   and value.get("binding") == binding.get("binding")
                   and value.get("context") == binding.get("context") == context
                   and context.get("root") == str(Path(root).absolute())
                   and context.get("session") == Path(root).name
                   and value.get("status") == "ready", "calendar_receipt_invalid")
    count = value.get("calendar_calls")
    policy.require(type(count) is int and count in (0, 1) and count == binding.get("calendar_calls")
                   and value.get("analysis_max_calls") == binding.get("analysis_max_calls") == context.get("session_max_calls", 60) - context.get("session_identity_calls", 0) - count,
                   "calendar_receipt_invalid")
    if count:
        admission, admission_digest = read_json(Path(root) / "calendar-admission.json", boundary=root)
        policy.require(admission_digest == value.get("admission_sha256")
                       and admission == {"contract": policy.CONTRACT, "binding": value["binding"],
                           "calendar_calls": 1, "combined_max_calls": context.get("session_max_calls", 60),
                           "context": context}, "calendar_receipt_invalid")
    else:
        policy.require(value.get("admission_sha256") is None
                       and not (Path(root) / "calendar-admission.json").exists()
                       and value.get("result", {}).get("action") == "reused", "calendar_receipt_invalid")
    return count


def _evidence(publication, revision=None, ingestion=None):
    sessions = tuple((row.session_date.isoformat(), row.status.value) for row in publication.sessions)
    digest = sha256(publication.raw_payload).hexdigest()
    revision = revision or policy.revision_for(publication.provider.provider_dataset_id, digest, sessions)
    return policy.Evidence(publication.provider.provider_dataset_id, publication.observed_at,
        publication.raw_payload, digest, sessions, revision,
        ingestion or policy.ingestion_for(revision, publication.observed_at))


@contextmanager
def _existing_store(settings, remaining, source_root):
    """Writable existing store only; bypass neither schema nor payload ownership checks."""
    from tradingagents.budget_readiness import _database_state
    from tradingagents.tushare_acceptance import validate_schema

    from .schema import SCHEMA_VERSION
    from .store import MarketHistoryStore

    regular(settings.database_path, boundary=source_root)
    _database_state(settings.database_path)
    regular(settings.database_path.with_name(settings.database_path.name + ".payload-mutation.sqlite3"),
            boundary=source_root, required=False)
    owner = regular(settings.payload_root / ".market-history-database-owner", boundary=source_root)
    policy.require(owner.read_bytes() == os.path.normcase(str(settings.database_path)).encode() + b"\n",
                   "calendar_payload_owner_mismatch")
    connection = sqlite3.connect(settings.database_path.as_uri() + "?mode=rw", uri=True,
                                 isolation_level=None, timeout=min(30, remaining()))
    try:
        connection.execute("PRAGMA foreign_keys=ON")
        policy.require(connection.execute("PRAGMA journal_mode").fetchone() == ("wal",),
                       "calendar_store_mode_unsupported")
        validate_schema(connection, SCHEMA_VERSION)
        yield MarketHistoryStore(settings, connection)
    finally:
        connection.close()


class CalendarStore:
    def __init__(self, config, remaining, source_root):
        self.config, self.settings = config, checked_settings(config, source_root)
        self.source_root = source_root
        self.remaining, self.head, self.publication = remaining, None, None

    def _read(self, store, request):
        from .calendar_discovery import _read_verified_membership
        from .current_cache import verify_prepared_exchange_revision
        membership = _read_verified_membership(store, request.start, request.analysis_date)
        policy.require(all(calendar.observed_at <= datetime.now(timezone.utc)
                           for _, calendar in membership.values()), "calendar_future_observation")
        rows = tuple(store._connection.execute(
            "SELECT calendar_revision_id,observed_at FROM market_session_calendars "
            "WHERE provider_dataset_id=? ORDER BY observed_at DESC,calendar_revision_id",
            (request.source_id,),
        ).fetchall())
        latest = None
        for revision, observed in rows:
            if observed != rows[0][1]:
                break
            current = _evidence(verify_prepared_exchange_revision(store, revision, request.exchange),
                                revision=revision)
            if latest is not None:
                policy.require(current.sessions == latest.sessions, "calendar_latest_conflict")
            latest = current
        return rows, latest

    def read_snapshot(self, request):
        from tradingagents.budget_readiness import _readonly_history
        self.remaining()
        with _readonly_history(self.config) as store:
            self.head, latest = self._read(store, request)
        self.remaining()
        return policy.Snapshot(sha256(encoded(self.head)).hexdigest() if latest else None, latest)

    def compare_and_publish(self, expected, candidate, request, check):
        from .calendar import CalendarPublicationConflictError
        from .current_cache import verify_prepared_exchange_revision
        expected_token = sha256(encoded(self.head)).hexdigest() if self.head else None
        policy.require(expected == expected_token and self.publication is not None
                       and _evidence(self.publication) == candidate, "calendar_publication_binding_mismatch")
        try:
            with _existing_store(self.settings, self.remaining, self.source_root) as store:
                def guard():
                    check()
                    _, newest = self._read(store, request)
                    policy.require(newest == candidate, "calendar_publication_verification_failed")
                    policy.require(policy.validate(newest, request, datetime.now(timezone.utc)),
                                   "calendar_publication_verification_failed")
                    check()
                published = store.publish_session_calendar(self.publication,
                    expected_head=self.head, precommit_guard=guard)
                verified = verify_prepared_exchange_revision(store, published.calendar_revision_id, request.exchange)
                return _evidence(verified, published.calendar_revision_id, published.ingestion_run_id)
        except CalendarPublicationConflictError:
            raise policy.CalendarStopped("calendar_concurrent_publication") from None


def prepare_for_session(config, *, identity, identity_revision, as_of, root, source_root, clock, prior_readiness,
                        confirmed=False):
    """Called only after the bounded entry's expanded START and identity precheck."""
    policy.require(confirmed is True, "calendar_confirmation_required")
    policy.require(policy.iso_day(as_of) <= datetime.now(policy.SHANGHAI).date(), "future_analysis_date")
    policy.require(all(issue.location == 'calendar' and issue.reason in {
        'incomplete_natural_date_window', 'corrupt_or_unreadable'} for issue in prior_readiness.issues),
        'calendar_precheck_not_repairable')
    from .config import provider_request_authority_config
    from .current_cache import _trusted_calendar_exchange, prepared_exchange_calendar_provider
    _trusted_calendar_exchange(identity.symbol, config)
    settings = checked_settings(config, source_root)
    context = expected_context(config, root=root, source_root=source_root, symbol=identity.symbol,
                               identity_revision=identity_revision, venue=identity.venue, as_of=as_of)
    source = prepared_exchange_calendar_provider({"XSHG":"SSE","XSHE":"SZSE"}[identity.venue])
    binding = sha256(encoded({"database": str(settings.database_path),
        "payload_root": str(settings.payload_root),
        "coordinator": str(provider_request_authority_config(settings).database_path)})).hexdigest()
    if not prior_readiness.issues:
        # Preserve the existing ready generic/SZSE path; no new source permission.
        ledger = RunLedger(root, (policy.CONTRACT, Path(root).name, identity.symbol,
            identity_revision, "existing-verified-calendar", binding, "existing-five-year-precheck",
            as_of, "no-request", identity.venue), context)
        clock.remaining()
        return ledger.complete({"status": "ready", "action": "reused", "physical_attempts": 0,
            "analysis_date": as_of, "readiness_sha256": sha256(encoded({
                "issues": [], "checked": [vars(item) for item in prior_readiness.checked]})).hexdigest()})
    allow_szse=False
    if identity.venue=="XSHE":
        from tradingagents.full_session_ledger import owner
        from full_financial_channel import binding as read_owner, effective_day
        ledger_owner=owner()
        if ledger_owner is not None and ledger_owner.child_role=="calendar":
            spec=read_owner(source_root,root)
            allow_szse=(ledger_owner.params==spec["params"] and ledger_owner.key==Path(root).name
                and ledger_owner.path==Path(root)/"physical-ledger/ledger.sqlite3"
                and spec["params"].get("contract")=="default-full-latest-intake-v1"
                and spec["params"].get("ticker")==identity.symbol and spec["params"].get("venue")=="XSHE"
                and effective_day(spec,root)==as_of)
    if allow_szse:
        context=expected_context(config, root=root, source_root=source_root, symbol=identity.symbol,
            identity_revision=identity_revision, venue=identity.venue, as_of=as_of, szse_refresh=True)
    request = policy.Request.create(session=Path(root).name, symbol=identity.symbol,
        identity_revision=identity_revision, source_id=source.provider_dataset_id,
        store_binding=binding, venue=identity.venue, currency=identity.currency,
        kind=identity.instrument_kind.value, analysis_date=as_of, allow_szse=allow_szse, now=datetime.now(timezone.utc))
    ledger = RunLedger(root, request.binding, context)
    store = CalendarStore(config, clock.remaining, source_root)

    def acquire(selected, gate):
        from tradingagents import tushare_https
        from tradingagents.dataflows.tushare_market_history import load_calendar
        from tradingagents.market_history import coordinator
        from tradingagents.picker.pit_config import PITConfig
        from tradingagents.picker.tushare_provider import TushareProvider
        from tradingagents.statement_acceptance import _require_shared_ceiling
        from tradingagents.tushare_acceptance import require_imported_authority
        token = os.environ.get("TUSHARE_TOKEN")
        policy.require(type(token) is str and bool(token), "calendar_token_required")
        from tradingagents.full_session_ledger import owner as session_owner
        active=session_owner()
        gate.record_at_send=bool(active and active.child_role=="calendar"
            and active.params.get("contract")=="default-full-latest-intake-v1")
        client = tushare_https.create_https_client(token, calendar_gate=gate)
        provider = TushareProvider.create(PITConfig(token, Path(root), 40, {"trade_cal": 40}, max_attempts=1),
                                         client=client)
        authority = provider_request_authority_config(settings)
        with _existing_store(authority, clock.remaining, source_root) as request_store:
            require_imported_authority(request_store._connection, settings.database_path)
            owner = coordinator.ProviderRequestCoordinator(request_store)
            _require_shared_ceiling(owner.configured_operator_ceiling(source.upstream_service_id))
            def now():
                # The coordinator also uses this clock while closing failed sequences.
                return datetime.now(timezone.utc)
            def sleep(seconds):
                paced_wait(seconds, gate=gate, clock=clock)
            acquired = []
            def workflow(execute):
                def page(endpoint, operation):
                    policy.require(endpoint == "trade_cal", "calendar_request_scope_mismatch")
                    def query():
                        gate.check()
                        if not gate.record_at_send:
                            coordinator.record_active_physical_attempt_io()
                        from tradingagents.picker.errors import TushareRateLimitError
                        try:
                            return operation()
                        except TushareRateLimitError as error:
                            delay = error.retry_after_seconds
                            raise coordinator.PhysicalAttemptFailure(
                                outcome=coordinator.PhysicalAttemptOutcome.RATE_LIMITED,
                                retryable=False, status_code=429, error_code="calendar_rate_limited",
                                retry_after_seconds=delay if delay and 0 < delay <= 31536000 else 300,
                            ) from None
                    return execute(endpoint, query)
                publication = load_calendar(selected.symbol, selected.start.isoformat(),
                    selected.analysis_date.isoformat(), provider=provider, request_executor=page)
                from .current_cache import bind_prepared_publication
                publication=bind_prepared_publication(publication, selected.exchange)
                acquired.append(publication)
                evidence = _evidence(publication)
                # The coordinator's closed codec supports this primitive envelope.
                return {'revision': evidence.revision, 'payload_sha256': evidence.payload_digest,
                        'source_id': evidence.source_id, 'observed_at': evidence.observed_at.isoformat()}
            request_key = 'calendar-prepare:' + selected.session
            try:
                envelope = owner.execute_physical_workflow(request_key=request_key,
                    upstream_service_id=source.upstream_service_id, owner_id="calendar:" + selected.session,
                    priority=coordinator.RequestPriority.INTERACTIVE_MAINLAND, now=now, sleep=sleep,
                    lease_duration=timedelta(minutes=2), max_physical_operations=1,
                    workflow=workflow, wait_for_initial_pacing=True, cooldown_scope="market-snapshot").value
            except BaseException as primary:
                try:
                    owner.cancel_queued_request(request_key=request_key,
                                                upstream_service_id=source.upstream_service_id)
                except BaseException as cleanup:
                    raise cleanup_failure(primary, cleanup, gate) from None
                gate.check()  # Restore a terminal gate code after coordinator cleanup.
                raise
            policy.require(len(acquired) == 1, 'calendar_acquisition_handoff_missing')
            value = acquired[0]
            evidence = _evidence(value)
            policy.require(envelope == {'revision': evidence.revision, 'payload_sha256': evidence.payload_digest,
                'source_id': evidence.source_id, 'observed_at': evidence.observed_at.isoformat()},
                'calendar_acquisition_handoff_mismatch')
        store.publication = value
        return evidence

    result = policy.prepare(request, policy.Consent(request.binding, True, True, True), store=store,
        acquire=acquire, ledger=ledger, wall_clock=lambda: datetime.now(timezone.utc),
        monotonic=time.monotonic, deadline=clock.deadline_monotonic)
    clock.remaining()
    return ledger.complete(result)

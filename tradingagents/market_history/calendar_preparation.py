"""Calendar-only preparation policy. No client, environment or storage construction.

Candidate only: production adapters and bounded-entry integration are not installed.
Ports must supply authenticated identity/source, a read-only verified store view,
one physical send per gate invocation, and an atomic compare/verify/append commit.
The in-memory contract tests do not qualify those production ports.
"""
from __future__ import annotations

import json
import math
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from hashlib import sha256

from .revisions import RevisionKind, revision_identity

DESTINATION = "https://api.waditu.com/dataapi/trade_cal"
NORMALIZER = "tushare-market-history-normalizer-v1"
ADJUSTMENT = "tushare-adj-factor-divide-end-anchor-qfq-v1"
CONTRACT = "bounded-calendar-prepare-v1"
SHANGHAI = timezone(timedelta(hours=8))  # Current wall date only; never infer session membership.
MAX_BYTES = 16 * 1024 * 1024


class CalendarStopped(RuntimeError):
    """Fixed local code; never include provider bodies or credential text."""

    def __init__(self, code, *, details=None):
        self.code = code
        self.details = details
        super().__init__(code)


def require(condition, code):
    if not condition:
        raise CalendarStopped(code)


def utc(value):
    require(type(value) is datetime and value.tzinfo is not None
            and value.utcoffset() is not None, "calendar_time_invalid")
    return value.astimezone(timezone.utc)


def iso_day(text):
    require(type(text) is str, "calendar_date_invalid")
    try:
        result = date.fromisoformat(text)
    except ValueError:
        raise CalendarStopped("calendar_date_invalid") from None
    require(result.isoformat() == text, "calendar_date_invalid")
    return result


@dataclass(frozen=True)
class Request:
    session: str
    symbol: str
    identity_revision: str
    source_id: str
    store_binding: str
    analysis_date: date
    start: date
    selected_at: datetime
    exchange: str = "SSE"

    @classmethod
    def create(cls, *, session, symbol, identity_revision, source_id, store_binding,
               venue, currency, kind, analysis_date, now, allow_szse=False):
        selected = utc(now)
        end = iso_day(analysis_date)
        require(end <= selected.astimezone(SHANGHAI).date(), "future_analysis_date")
        require(end.year > 5, "calendar_window_invalid")
        require(currency=="CNY" and kind=="equity" and (venue=="XSHG" or (venue=="XSHE" and allow_szse is True)),
                "calendar_refresh_exchange_unsupported")
        require(type(symbol) is str and len(symbol) == 9 and symbol[:6].isascii()
                and symbol[:6].isdigit() and symbol[6:] in ({".SZ"} if venue=="XSHE" else {".SH", ".SS"}),
                "calendar_identity_mismatch")
        require(all(type(value) is str and value for value in
                    (session, identity_revision, source_id, store_binding)),
                "calendar_binding_missing")
        # Same five-calendar-year rule as relativedelta(years=5), including Feb 29.
        try:
            start = end.replace(year=end.year - 5)
        except ValueError:
            start = end.replace(year=end.year - 5, day=28)
        return cls(session, symbol, identity_revision, source_id, store_binding,
                   end, start, selected, "SZSE" if venue=="XSHE" else "SSE")

    @property
    def binding(self):
        return (CONTRACT, self.session, self.symbol, self.identity_revision,
                self.source_id, self.store_binding, self.start.isoformat(),
                self.analysis_date.isoformat(), DESTINATION, self.exchange)


@dataclass(frozen=True)
class Consent:
    binding: tuple
    confirmed: bool
    allow_request: bool
    allow_publish: bool
    max_calendar_attempts: int = 1


@dataclass(frozen=True)
class Evidence:
    """Adapter-attested metadata plus revalidated payload/membership identities."""
    source_id: str
    observed_at: datetime
    raw_payload: bytes
    payload_digest: str
    sessions: tuple  # Exact (ISO date, 'open' or 'closed') pairs.
    revision: str
    ingestion_revision: str


@dataclass(frozen=True)
class Snapshot:
    """Opaque head token covers latest-source selection, including equal-time conflicts."""
    head_token: str | None
    evidence: Evidence | None


def revision_for(source_id, digest, sessions):
    return revision_identity(RevisionKind.MARKET_SESSION_CALENDAR, {
        "payload_digest": digest, "provider_dataset_id": source_id,
        "reference_market": "mainland-cn", "timezone_name": "Asia/Shanghai",
        "sessions": [{"session_date": day, "status": status,
                      "opens_at": None, "closes_at": None} for day, status in sessions],
    })


def ingestion_for(revision, observed_at):
    return revision_identity(RevisionKind.INGESTION_RUN, {
        "calendar_revision_id": revision,
        "observed_at": utc(observed_at).isoformat(), "operation": "calendar_refresh",
    })


def unique_object(pairs):
    value = {}
    for key, item in pairs:
        require(key not in value, "calendar_payload_schema")
        value[key] = item
    return value


def validate(evidence, request, now, *, acquired_after=None):
    require(type(evidence) is Evidence, "calendar_evidence_invalid")
    require(evidence.source_id == request.source_id, "calendar_source_mismatch")
    observed = utc(evidence.observed_at)
    require(observed <= utc(now), "calendar_future_observation")
    if acquired_after is not None:
        require(observed >= utc(acquired_after), "calendar_acquisition_time_invalid")
    require(type(evidence.raw_payload) is bytes and 0 < len(evidence.raw_payload) <= MAX_BYTES,
            "calendar_payload_invalid")
    require(sha256(evidence.raw_payload).hexdigest() == evidence.payload_digest,
            "calendar_payload_digest_mismatch")
    try:
        payload = json.loads(evidence.raw_payload, object_pairs_hook=unique_object)
    except (ValueError, UnicodeError, RecursionError):
        raise CalendarStopped("calendar_payload_schema") from None
    require(type(payload) is dict and set(payload) == {
        "calendar", "normalizer_version", "adjustment_methodology"}, "calendar_payload_schema")
    require(payload["normalizer_version"] == NORMALIZER
            and payload["adjustment_methodology"] == ADJUSTMENT, "calendar_payload_schema")
    rows = payload["calendar"]
    require(type(rows) is list and 0 < len(rows) < 5000, "calendar_payload_schema")
    membership = {}
    for row in rows:
        require(type(row) is dict and set(row) in (
            {"exchange", "cal_date", "is_open"},
            {"exchange", "cal_date", "is_open", "pretrade_date"}),
                "calendar_payload_schema")
        require(row["exchange"] == request.exchange, "calendar_source_mismatch")
        text = row["cal_date"]
        require(type(text) is str and len(text) == 8 and text.isascii() and text.isdigit(),
                "calendar_payload_schema")
        day = iso_day(text[:4] + "-" + text[4:6] + "-" + text[6:])
        previous = row.get("pretrade_date")
        if previous is not None:
            require(type(previous) is str and len(previous) == 8
                    and previous.isascii() and previous.isdigit(), "calendar_payload_schema")
            iso_day(previous[:4] + "-" + previous[4:6] + "-" + previous[6:])
        require(type(row["is_open"]) is str and row["is_open"] in {"0", "1"},
                "calendar_membership_invalid")
        require(day not in membership, "calendar_duplicate_date")
        membership[day] = "open" if row["is_open"] == "1" else "closed"
    days = sorted(membership)
    require(len(days) == (days[-1] - days[0]).days + 1, "calendar_internal_gap")
    sessions = tuple((day.isoformat(), membership[day]) for day in days)
    require(type(evidence.sessions) is tuple and evidence.sessions == sessions,
            "calendar_payload_membership_mismatch")
    require(evidence.revision == revision_for(evidence.source_id, evidence.payload_digest, sessions),
            "calendar_revision_mismatch")
    require(evidence.ingestion_revision == ingestion_for(evidence.revision, observed),
            "calendar_ingestion_mismatch")
    return days[0] <= request.start and days[-1] >= request.analysis_date


class PhysicalGate:
    """Invoke at the physical HTTPS send seam, NOT once around a paginating SDK.

    Shared ledger admission must persist once per session before I/O and carry into analysis; the
    local cap is an additional limit, never another quota. No retry in this module.
    """
    def __init__(self, request, ledger, monotonic, deadline):
        require(type(deadline) in (int, float) and math.isfinite(deadline),
                "calendar_deadline_invalid")
        self.request, self.ledger, self.clock, self.deadline = request, ledger, monotonic, deadline
        self.attempts = 0
        self.fatal = None

    def stop(self, code):
        self.fatal = self.fatal or code
        raise CalendarStopped(self.fatal)

    def check(self):
        if self.fatal is not None:
            raise CalendarStopped(self.fatal)
        instant = self.clock()
        if type(instant) not in (int, float) or not math.isfinite(instant) or instant >= self.deadline:
            self.stop("calendar_deadline_exceeded")
        return min(30.0, self.deadline - instant)

    def send(self, *, destination, exchange, start, end, limit, offset, operation):
        timeout = self.check()
        if (destination, exchange, start, end, limit, offset) != (
            DESTINATION, self.request.exchange, self.request.start.isoformat().replace("-", ""),
            self.request.analysis_date.isoformat().replace("-", ""), 5000, 0
        ) or type(limit) is not int or type(offset) is not int:
            self.stop("calendar_request_scope_mismatch")
        if self.attempts >= 1:
            self.stop("calendar_request_limit")
        try:
            self.ledger.admit_calendar_once(self.request.binding)
        except CalendarStopped as error:
            self.stop(error.code)
        except Exception:
            self.stop("calendar_admission_outcome_unknown")
        timeout = self.check()  # Admission/pacing may have consumed the remaining time.
        self.attempts += 1  # Failed sends consume the same fixed budget.
        try:
            result = operation(timeout)
        except TimeoutError:
            self.stop("calendar_provider_timeout")
        except CalendarStopped as error:
            self.stop(error.code)
        except Exception as error:
            from tradingagents.tushare_https import TushareHTTPSFailure
            if isinstance(error, TushareHTTPSFailure) and error.status_code == 429:
                raise
            self.stop("calendar_provider_failed")
        self.check()
        return result


def prepare(request, consent, *, store, acquire, ledger, wall_clock, monotonic, deadline):
    """Reuse or acquire a full immutable SSE revision; never prices or analysis.

    store.read_snapshot must reject schema/source/payload/conflict failures without
    converting them to missing data. compare_and_publish must compare the head,
    check the supplied guard and fully verify the candidate IN its transaction,
    then append atomically while retaining old revisions. A thrown unknown commit
    outcome is terminal, never authorization to publish again.
    """
    require(type(request) is Request and type(consent) is Consent, "calendar_consent_invalid")
    now = utc(wall_clock())
    require(type(request.start) is date and type(request.analysis_date) is date,
            "calendar_window_invalid")
    canonical_request = Request.create(
        session=request.session, symbol=request.symbol, identity_revision=request.identity_revision,
        source_id=request.source_id, store_binding=request.store_binding, venue="XSHE" if request.exchange=="SZSE" else "XSHG",
        allow_szse=request.exchange=="SZSE",
        currency="CNY", kind="equity", analysis_date=request.analysis_date.isoformat(),
        now=request.selected_at)
    require(canonical_request == request, "calendar_window_invalid")
    require(utc(request.selected_at) <= now, "calendar_time_invalid")
    require(request.analysis_date <= now.astimezone(SHANGHAI).date(), "future_analysis_date")
    require(consent.confirmed is True and consent.binding == request.binding,
            "calendar_confirmation_required")
    gate = PhysicalGate(request, ledger, monotonic, deadline)
    gate.check()
    try:
        snapshot = store.read_snapshot(request)
    except CalendarStopped:
        raise
    except Exception:
        raise CalendarStopped("calendar_store_unreadable") from None
    require(type(snapshot) is Snapshot and (
        type(snapshot.head_token) is str if snapshot.evidence is not None else snapshot.head_token is None),
        "calendar_store_state_invalid")
    if snapshot.evidence is not None and validate(snapshot.evidence, request, wall_clock()):
        gate.check()
        return {"status": "ready", "action": "reused", "revision": snapshot.evidence.revision,
                "analysis_date": request.analysis_date.isoformat(), "physical_attempts": 0}
    require(consent.allow_request is True and consent.allow_publish is True
            and type(consent.max_calendar_attempts) is int and consent.max_calendar_attempts == 1,
            "calendar_preparation_not_authorized")
    acquired_after = utc(wall_clock())
    try:
        candidate = acquire(request, gate)
    except CalendarStopped:
        raise
    except Exception:
        raise CalendarStopped("calendar_acquisition_failed") from None
    gate.check()
    require(gate.attempts == 1, "calendar_physical_receipt_missing")
    require(validate(candidate, request, wall_clock(), acquired_after=acquired_after),
            "calendar_acquisition_incomplete")
    require(candidate.sessions[0][0] == request.start.isoformat()
            and candidate.sessions[-1][0] == request.analysis_date.isoformat(),
            "calendar_acquisition_scope_mismatch")
    gate.check()
    try:
        committed = store.compare_and_publish(snapshot.head_token, candidate, request, gate.check)
    except CalendarStopped:
        raise
    except Exception:
        raise CalendarStopped("calendar_publish_outcome_unknown") from None
    gate.check()
    require(type(committed) is Evidence and committed == candidate,
            "calendar_publication_verification_failed")
    require(validate(committed, request, wall_clock(), acquired_after=acquired_after),
            "calendar_publication_verification_failed")
    gate.check()
    return {"status": "ready", "action": "published", "revision": committed.revision,
            "analysis_date": request.analysis_date.isoformat(), "physical_attempts": gate.attempts}

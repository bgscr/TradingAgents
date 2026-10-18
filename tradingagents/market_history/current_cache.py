"""Exact-window current-analysis cache. This does not confer strict PIT eligibility."""
from __future__ import annotations

import json
from dataclasses import asdict, dataclass, replace
from datetime import date, datetime, timedelta
from decimal import Decimal
from hashlib import sha256
from pathlib import Path

from pydantic import TypeAdapter

from tradingagents.dataflows.config import get_config
from tradingagents.dataflows.symbol_utils import resolve_mainland_instrument
from tradingagents.evidence import SourceAcquisitionOutcome, SourceArtifact
from tradingagents.market_history.calendar import MarketSessionCalendarPublication
from tradingagents.market_history.config import MarketHistoryConfig, MarketHistoryMode
from tradingagents.market_history.coordinator import upstream_service_identity_for_provider
from tradingagents.market_history.current import _analysis_as_of, _now
from tradingagents.market_history.models import (
    InstrumentSpec,
    ProvenanceClass,
    ProviderDatasetSpec,
    TradingStatus,
    TradingStatusProvenance,
)
from tradingagents.market_history.store import MarketHistoryStore, _unique_json_object

CONTRACT = "mainland-current-cache-v1"
SSE_CALENDAR_PROOF = "tushare-sse-current-calendar-v1"
SSE_CALENDAR_SYMBOL = "601328.SS"
MANIFEST_CONTRACT = "current-cache-compatibility-manifest-v1"
MANIFEST_PATH = Path(__file__).with_name("current_cache_compatibility.json")
SOURCE_ROOT = Path(__file__).resolve().parents[2]
SOURCE_FILES = (
    "tradingagents/market_history/current_cache.py",
    "tradingagents/market_history/current_incremental.py",
    "tradingagents/market_history/config.py",
    "tradingagents/market_history/store.py",
    "tradingagents/market_history/shadow.py",
    "tradingagents/market_history/current.py",
    "tradingagents/market_history/calendar_discovery.py",
    "tradingagents/market_history/calendar.py",
    "tradingagents/market_history/frames.py",
    "tradingagents/market_history/models.py",
    "tradingagents/market_history/revisions.py",
    "tradingagents/market_history/schema.py",
    "tradingagents/market_history/snapshot_identity.py",
    "tradingagents/market_history/maintenance.py",
    "tradingagents/market_history/bundle.py",
    "tradingagents/market_history/coordinator.py",
    "tradingagents/dataflows/market_snapshot.py",
    "tradingagents/dataflows/acquisition.py",
    "tradingagents/dataflows/stockstats_utils.py",
    "tradingagents/dataflows/symbol_utils.py",
    "tradingagents/dataflows/baostock_data.py",
    "tradingagents/dataflows/baostock_calendar.py",
    "tradingagents/dataflows/tushare_market_history.py",
    "tradingagents/picker/tushare_provider.py",
    "tradingagents/tushare_https.py",
    "tradingagents/dataflows/akshare_data.py",
    "tradingagents/dataflows/y_finance.py",
    "tradingagents/strategy_registry.py",
    "tradingagents/decision_policy.py",
    "tradingagents/agents/schemas.py",
    "tradingagents/evidence.py",
)
VERIFICATION_FILES = (
    "tests/test_tushare_current_incremental.py",
    "tests/test_tushare_current_acceptance.py",
    "scripts/accept_tushare_current.py",
    "tradingagents/tushare_acceptance.py",
    "tests/test_market_history_persistent_reuse.py",
    "tests/test_tushare_pit_provider.py",
    "scripts/run_current_cache_compatibility.py",
)
SCENARIOS = (
    "shanghai_primary", "shenzhen_primary", "baostock_fallback", "yahoo_fallback",
    "confirmed_suspension", "representation_noise", "in_range_gap", "factor_revision",
    "rate_limit_fallback", "store_corruption_degradation", "concurrent_write", "crash_recovery",
)


def canonical(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)


def digest(value):
    return sha256(canonical(value).encode("utf-8")).hexdigest()


def source_fingerprints(*, include_verification=True):
    # Only UTF-8 decoding and CRLF-to-LF normalization; no semantic filtering.
    paths = SOURCE_FILES + VERIFICATION_FILES if include_verification else SOURCE_FILES
    return {name: sha256((SOURCE_ROOT / name).read_bytes().decode("utf-8").replace(
        "\r\n", "\n").encode("utf-8")).hexdigest() for name in paths}


def compatibility_diagnostic():
    """Fail closed independently of strict-history/qualification DB records."""
    try:
        manifest = json.loads(MANIFEST_PATH.read_text(encoding="utf-8"),
                              object_pairs_hook=_unique_json_object)
        if set(manifest) != {"contract", "mechanism", "sources", "matrix", "matrix_sha256", "evidence", "details"}:
            raise ValueError("shape")
        if manifest["contract"] != MANIFEST_CONTRACT or manifest["mechanism"] != CONTRACT:
            raise ValueError("version")
        sources = manifest["sources"]
        if (set(sources) != set(SOURCE_FILES + VERIFICATION_FILES)
                or {name: sources[name] for name in SOURCE_FILES} != source_fingerprints(include_verification=False)
                or any(len(sources[name]) != 64 or any(char not in "0123456789abcdef" for char in sources[name])
                       for name in VERIFICATION_FILES)):
            raise ValueError("source_mismatch")
        matrix = manifest["matrix"]
        if set(matrix) != set(SCENARIOS) or digest(matrix) != manifest["matrix_sha256"]:
            raise ValueError("matrix")
        if set(manifest["details"]) != set(SCENARIOS):
            raise ValueError("details")
        for name, result in matrix.items():
            if (set(result) != {"passed", "evidence_sha256"} or result["passed"] is not True
                    or not isinstance(result["evidence_sha256"], str)
                    or len(result["evidence_sha256"]) != 64
                    or any(char not in "0123456789abcdef" for char in result["evidence_sha256"])):
                raise ValueError("scenario")
            proof = manifest["details"][name]
            if (digest(proof) != result["evidence_sha256"] or proof["live"] != proof["restored"]
                    or not proof["lineage"] or proof["rows"] < 21):
                raise ValueError("scenario_evidence")
        if (set(manifest["evidence"]) != {"path", "sha256", "scope"}
                or manifest["evidence"]["scope"] != "isolated-synthetic-current-cache-only"
                or len(manifest["evidence"]["sha256"]) != 64):
            raise ValueError("evidence")
        report = {"matrix": matrix, "details": manifest["details"],
                  "verification_sources": {name: sources[name] for name in VERIFICATION_FILES},
                  "scope": "isolated-synthetic-current-cache-only", "mechanism_phase": "direct-codec-before-manifest"}
        if digest(report) != manifest["evidence"]["sha256"]:
            raise ValueError("evidence_digest")
    except Exception as exc:
        return f"current_cache_gate_denied:{type(exc).__name__}:{exc}"
    return None


def binding(symbol, provider_name, descriptor, *, store=None):
    instrument = resolve_mainland_instrument(symbol)
    version = descriptor.current_cache_version
    if instrument is None or not version or provider_name not in {"akshare", "baostock", "tushare", "yfinance"}:
        raise ValueError("current cache provider/instrument is not registered")
    upstream, service = upstream_service_identity_for_provider(provider_name)
    dataset = f"{CONTRACT}:{version}"
    provider = ProviderDatasetSpec(upstream, service, "provider-dataset=sha256:" + digest(
        [upstream, provider_name, dataset, descriptor.adjustment_basis]), provider_name,
        dataset, descriptor.adjustment_basis, False)
    identity = InstrumentSpec("instrument=sha256:" + digest([
        instrument.yahoo_symbol, instrument.exchange, instrument.instrument_kind, "CNY", "mainland-routing-v1"]),
        instrument.yahoo_symbol, instrument.exchange, instrument.instrument_kind, "CNY", "mainland-routing-v1")
    if store is not None:
        row = store._connection.execute(
            "SELECT instrument_id,currency,identity_revision FROM instruments "
            "WHERE canonical_symbol=? AND reference_market=? AND instrument_kind=?",
            (identity.canonical_symbol, identity.reference_market, identity.instrument_kind),
        ).fetchone()
        if row is not None:
            if row[1:] != (identity.currency, identity.identity_revision):
                raise ValueError("current instrument identity conflicts with stored metadata")
            identity = _compatible_instrument_id(identity, row[0])
    return provider, identity


def _compatible_instrument_id(instrument, stored_id):
    # The legacy shadow writer and CURRENT used different deterministic keys
    # for the same unique (symbol, venue, kind). Preserve the existing entity;
    # never rewrite old evidence or accept arbitrary IDs/semantic aliases.
    from tradingagents.market_history.shadow import _stable_id

    legacy_id = _stable_id("instrument", instrument.canonical_symbol,
        instrument.reference_market, instrument.instrument_kind, instrument.currency)
    if stored_id not in {instrument.instrument_id, legacy_id}:
        raise ValueError("current instrument identity is not a known stored binding")
    return replace(instrument, instrument_id=stored_id)


_SNAPSHOT_FIELDS = (
    "symbol", "provider", "retrieved_at", "adjustment_basis", "requested_date",
    "effective_trading_date", "frame_sha256", "snapshot_id", "snapshot_id_version",
    "pin_membership_digest", "snapshot_manifest_json", "history_store_status",
    "current_tradeability", "latest_traded_close_diagnostic",
    "current_history_lineage",
)


@dataclass(frozen=True)
class _StoredSSECalendar(MarketSessionCalendarPublication):
    """Exact retained Tushare publication; CURRENT-only, not a CAL-02 pin."""

    calendar_revision_id: str = ""


def prepared_sse_calendar_provider():
    """Exact source family; identifying it does not admit it to other readers."""
    from tradingagents.dataflows.tushare_market_history import (
        CALENDAR_DATASET_NAME,
        NORMALIZER_VERSION,
        _identity,
    )

    upstream, service = upstream_service_identity_for_provider("tushare")
    return ProviderDatasetSpec(upstream, service, _identity("provider-dataset", upstream,
        CALENDAR_DATASET_NAME, "not-applicable", NORMALIZER_VERSION, "SSE"),
        "tushare", CALENDAR_DATASET_NAME, "not-applicable", False)


def verify_prepared_sse_revision(store, revision_id):
    """Authenticate one immutable source before admission OR source exclusion.

    This integrity check grants no instrument/mode eligibility. Callers must
    enforce their own policy; the BaoStock reader only excludes this source.
    """
    from tradingagents.dataflows.tushare_market_history import (
        CALENDAR_DATASET_NAME,
        NORMALIZER_VERSION,
    )
    from tradingagents.market_history.calendar import MarketSession, MarketSessionStatus
    from tradingagents.market_history.revisions import RevisionKind, revision_identity

    expected = prepared_sse_calendar_provider()
    upstream, service = expected.upstream_service_id, expected.upstream_service_name
    connection = store._connection
    row = connection.execute(
        "SELECT c.calendar_revision_id,c.reference_market,c.timezone_name,c.observed_at,"
        "c.first_observed_at,c.provenance_class,c.payload_digest,c.ingestion_run_id,"
        "d.provider_name,d.upstream_service_id,d.dataset_name,d.adjustment_methodology,"
        "d.strict_history_qualified,u.service_name,u.account_scope,c.provider_dataset_id "
        "FROM market_session_calendars c LEFT JOIN provider_datasets d "
        "ON c.provider_dataset_id=d.provider_dataset_id LEFT JOIN upstream_services u "
        "ON d.upstream_service_id=u.upstream_service_id WHERE c.calendar_revision_id=?",
        (revision_id,),
    ).fetchone()
    if (row is None or row[15] != expected.provider_dataset_id
            or row[1:3] != ("mainland-cn", "Asia/Shanghai") or row[4] != row[3]
            or row[5] != "retrospective_backfill"
            or row[8:15] != ("tushare", upstream, CALENDAR_DATASET_NAME,
                           "not-applicable", 0, service, "")):
        raise ValueError("prepared SSE calendar source mismatch")
    raw = store.read_payload(row[6])
    session_rows = connection.execute(
        "SELECT session_date,session_status,opens_at,closes_at FROM market_sessions "
        "WHERE calendar_revision_id=? ORDER BY session_date", (row[0],),
    ).fetchall()
    if not session_rows or any(item[2:] != (None, None) for item in session_rows):
        raise ValueError("prepared SSE calendar membership invalid")
    sessions = tuple(MarketSession(date.fromisoformat(item[0]), MarketSessionStatus(item[1]))
                     for item in session_rows)
    fields = [{"session_date": item.session_date.isoformat(), "status": item.status.value,
               "opens_at": None, "closes_at": None} for item in sessions]
    if (any(item[0] != session.session_date.isoformat() for item, session in zip(session_rows, sessions, strict=True))
            or sha256(raw).hexdigest() != row[6]
            or row[0] != revision_identity(RevisionKind.MARKET_SESSION_CALENDAR, {
                "payload_digest": row[6], "provider_dataset_id": expected.provider_dataset_id,
                "reference_market": row[1], "sessions": fields, "timezone_name": row[2]})):
        raise ValueError("prepared SSE calendar revision mismatch")
    run = connection.execute(
        "SELECT provider_dataset_id,instrument_id,operation,provenance_class,requested_start,"
        "requested_end,started_at,completed_at,status,payload_digest,error_code "
        "FROM ingestion_runs WHERE run_id=?", (row[7],),
    ).fetchone()
    if (row[7] != revision_identity(RevisionKind.INGESTION_RUN, {
            "calendar_revision_id": row[0], "observed_at": row[3], "operation": "calendar_refresh"})
            or run != (expected.provider_dataset_id, None, "calendar_refresh", row[5],
                       sessions[0].session_date.isoformat(), sessions[-1].session_date.isoformat(),
                       row[3], row[3], "published", row[6], None)):
        raise ValueError("prepared SSE calendar ingestion mismatch")
    dates = {item.session_date for item in sessions}
    first, last = sessions[0].session_date, sessions[-1].session_date
    if (len(dates) != (last-first).days+1):
        raise ValueError("prepared SSE calendar range incomplete")
    calendar = _StoredSSECalendar(provider=expected, reference_market=row[1], timezone_name=row[2],
        observed_at=datetime.fromisoformat(row[3]), provenance_class=ProvenanceClass.RETROSPECTIVE_BACKFILL,
        raw_payload=raw, sessions=sessions, payload_contract=SSE_CALENDAR_PROOF,
        endpoint="trade_cal", normalizer_version=NORMALIZER_VERSION, calendar_revision_id=row[0])
    validate_calendar_evidence(calendar_evidence(calendar))
    return calendar


def read_prepared_sse_calendar(store, symbol, start, end):
    """Read a separately prepared full SSE window for 601328, with no acquisition.

    A partial latest revision or corrupt source is a refusal, not permission to
    fall back to older data. No cross-revision union or new observation is made.
    Other instruments and history modes retain their existing calendar path.
    """
    instrument = resolve_mainland_instrument(symbol) if symbol else None
    if (store.config.mode is not MarketHistoryMode.CURRENT or instrument is None
            or instrument.yahoo_symbol != SSE_CALENDAR_SYMBOL):
        return None
    if store.config.data_usage_mode.value != "personal_research":
        raise ValueError("prepared SSE calendar usage not entitled")
    if type(start) is not date or type(end) is not date or start > end:
        raise ValueError("prepared SSE calendar range invalid")
    rows = store._connection.execute(
        "SELECT calendar_revision_id,observed_at FROM market_session_calendars "
        "WHERE provider_dataset_id=? ORDER BY observed_at DESC,calendar_revision_id",
        (prepared_sse_calendar_provider().provider_dataset_id,),
    ).fetchall()
    selected = None
    for revision_id, observed_at in rows:
        if selected is not None and observed_at != rows[0][1]:
            break
        calendar = verify_prepared_sse_revision(store, revision_id)
        if calendar.sessions[0].session_date > start or calendar.sessions[-1].session_date < end:
            raise ValueError("prepared SSE calendar range incomplete")
        if selected is not None and calendar.sessions != selected.sessions:
            raise ValueError("prepared SSE calendar equally recent conflict")
        selected = calendar
    return selected


def calendar_evidence(calendar):
    provider = getattr(calendar, "provider", None)
    if provider is not None:
        if provider.provider_name != "tushare":
            raise ValueError("current cache publication requires registered Tushare evidence")
        proof = {"kind": ("tushare-current-view-v1" if calendar.payload_contract == "tushare-current-view-v1"
                          else "tushare-response-v1"), "provider": asdict(provider),
                 "provenance": calendar.provenance_class.value,
                 "raw_payload": calendar.raw_payload.decode("utf-8")}
        if calendar.payload_contract == SSE_CALENDAR_PROOF:
            proof.update(kind=SSE_CALENDAR_PROOF, symbol_scope=SSE_CALENDAR_SYMBOL)
    else:
        proof = {"kind": "verified-calendar-view-v1", "manifest": calendar.view_manifest_json,
                 "provenance": calendar.provenance_class.value}
    return {
        "reference_market": calendar.reference_market,
        "timezone": calendar.timezone_name,
        "provider_dataset_id": provider.provider_dataset_id if provider else calendar.provider_dataset_id,
        "revision": getattr(calendar, "calendar_revision_id", None),
        "payload_sha256": (calendar.payload_digest or sha256(calendar.view_manifest_json.encode("utf-8")).hexdigest()),
        "observed_at": calendar.observed_at.isoformat(),
        "proof": proof,
        "sessions": [[item.session_date.isoformat(), item.status.value,
                      item.closes_at.isoformat() if item.closes_at else None] for item in calendar.sessions],
    }


def validate_calendar_evidence(evidence):
    """Recheck the retained provider response or exact immutable source revisions."""
    proof = evidence["proof"]
    observed = datetime.fromisoformat(evidence["observed_at"])
    if observed.tzinfo is None or observed > _now():
        raise ValueError("calendar observation is invalid")
    if proof["kind"] == "tushare-current-view-v1":
        from tradingagents.dataflows.tushare_market_history import history_binding
        from tradingagents.market_history.current_incremental import _materialize, read_retained
        raw = proof["raw_payload"].encode()
        manifest = json.loads(raw, object_pairs_hook=_unique_json_object)
        provider, instrument = history_binding(manifest["instrument"]["canonical_symbol"])
        if manifest["provider"] != asdict(provider) or manifest["instrument"] != asdict(instrument):
            raise ValueError("CURRENT view family binding")
        with MarketHistoryStore.open(MarketHistoryConfig.from_mapping(get_config())) as store:
            retained = read_retained(store, provider, instrument,
                bundle_revision_id=manifest["bundle_revision_id"],
                calendar_revision_ids=manifest["calendar_revision_ids"])
            if retained is None:
                raise ValueError("CURRENT view bundle missing")
            restored = _materialize(retained, instrument.canonical_symbol,
                date.fromisoformat(manifest["start"]), date.fromisoformat(manifest["end"]))
        if calendar_evidence(restored.calendar) != evidence:
            raise ValueError("CURRENT view immutable source closure mismatch")
    elif proof["kind"] in {"tushare-response-v1", SSE_CALENDAR_PROOF}:
        from tradingagents.dataflows.tushare_market_history import (
            ADJUSTMENT_METHODOLOGY,
            CALENDAR_DATASET_NAME,
            NORMALIZER_VERSION,
            _identity,
        )
        scoped = proof["kind"] == SSE_CALENDAR_PROOF
        keys = {"kind", "provider", "provenance", "raw_payload"} | ({"symbol_scope"} if scoped else set())
        if set(proof) != keys or proof["provenance"] != "retrospective_backfill":
            raise ValueError("Tushare calendar provenance")
        raw = proof["raw_payload"].encode("utf-8")
        if sha256(raw).hexdigest() != evidence["payload_sha256"]:
            raise ValueError("Tushare raw evidence digest")
        payload = json.loads(raw, object_pairs_hook=_unique_json_object)
        if payload["normalizer_version"] != NORMALIZER_VERSION or payload["adjustment_methodology"] != ADJUSTMENT_METHODOLOGY:
            raise ValueError("Tushare normalization version")
        dates = {}
        exchanges = set()
        for row in payload["calendar"]:
            text = row["cal_date"]
            if not isinstance(text, str) or len(text) != 8 or not text.isascii() or not text.isdigit():
                raise ValueError("Tushare calendar date")
            day = datetime.strptime(row["cal_date"], "%Y%m%d").date().isoformat()
            flag = Decimal(str(row["is_open"]))
            if day in dates or flag not in {Decimal(0), Decimal(1)}:
                raise ValueError("Tushare calendar membership")
            dates[day] = "open" if flag == 1 else "closed"
            exchanges.add(row["exchange"])
        if len(exchanges) != 1 or next(iter(exchanges)) not in {"SSE", "SZSE", "BSE"}:
            raise ValueError("Tushare calendar exchange")
        if scoped and (exchanges != {"SSE"} or proof["symbol_scope"] != SSE_CALENDAR_SYMBOL):
            raise ValueError("prepared SSE calendar scope")
        upstream, service = upstream_service_identity_for_provider("tushare")
        expected = ProviderDatasetSpec(upstream, service, _identity("provider-dataset", upstream,
            CALENDAR_DATASET_NAME, "not-applicable", NORMALIZER_VERSION, next(iter(exchanges))),
            "tushare", CALENDAR_DATASET_NAME, "not-applicable", False)
        if (proof["provider"] != asdict(expected) or evidence["provider_dataset_id"] != expected.provider_dataset_id
                or evidence["sessions"] != [[day, dates[day], None] for day in sorted(dates)]):
            raise ValueError("Tushare calendar source binding")
        if scoped:
            from tradingagents.market_history.revisions import RevisionKind, revision_identity
            expected_revision = revision_identity(RevisionKind.MARKET_SESSION_CALENDAR, {
                "payload_digest": evidence["payload_sha256"], "provider_dataset_id": expected.provider_dataset_id,
                "reference_market": evidence["reference_market"], "timezone_name": evidence["timezone"],
                "sessions": [{"session_date": day, "status": dates[day], "opens_at": None, "closes_at": None}
                             for day in sorted(dates)]})
            if evidence["revision"] != expected_revision:
                raise ValueError("prepared SSE calendar revision binding")
    elif proof["kind"] == "verified-calendar-view-v1":
        from tradingagents.market_history.calendar_discovery import (
            _calendar_view,
            _read_verified_revision,
        )
        if set(proof) != {"kind", "manifest", "provenance"}:
            raise ValueError("calendar view proof shape")
        if sha256(proof["manifest"].encode("utf-8")).hexdigest() != evidence["payload_sha256"]:
            raise ValueError("calendar view digest")
        manifest = json.loads(proof["manifest"], object_pairs_hook=_unique_json_object)
        with MarketHistoryStore.open(MarketHistoryConfig.from_mapping(get_config())) as store:
            sources = {identity: _read_verified_revision(store, identity) for identity in manifest["source_calendar_revision_ids"]}
        membership = {}
        for item in manifest["sessions"]:
            source = sources[item["calendar_revision_id"]]
            day = date.fromisoformat(item["session_date"])
            session = next(value for value in source.sessions if value.session_date == day)
            membership[day] = (session, source)
        view = _calendar_view(membership, date.fromisoformat(manifest["requested_start"]), date.fromisoformat(manifest["requested_end"]))
        if calendar_evidence(view) != evidence:
            raise ValueError("calendar view source closure mismatch")
    else:
        raise ValueError("unsupported calendar evidence")


def validate_coverage(frame, evidence, start, end, *, symbol=None):
    if set(evidence) != {"reference_market", "timezone", "provider_dataset_id", "revision",
                        "payload_sha256", "observed_at", "sessions", "proof"}:
        raise ValueError("calendar evidence shape")
    if (evidence["reference_market"] != "mainland-cn" or evidence["timezone"] != "Asia/Shanghai"
            or not evidence["provider_dataset_id"] or len(evidence["payload_sha256"]) != 64):
        raise ValueError("calendar identity")
    validate_calendar_evidence(evidence)
    if evidence["proof"]["kind"] == SSE_CALENDAR_PROOF:
        instrument = resolve_mainland_instrument(symbol) if symbol else None
        if instrument is None or instrument.yahoo_symbol != SSE_CALENDAR_SYMBOL:
            raise ValueError("prepared SSE calendar instrument scope")
    as_of = _analysis_as_of(date.fromisoformat(end), timezone_name=evidence["timezone"])
    sessions = {}
    for day, status, closes_at in evidence["sessions"]:
        if day in sessions or status not in {"open", "closed"}:
            raise ValueError("calendar membership")
        # Older provider calendars use the registered mainland regular close.
        close = datetime.fromisoformat(closes_at) if closes_at else datetime.fromisoformat(day + "T15:00:00+08:00")
        if close.tzinfo is None:
            raise ValueError("calendar close timezone")
        sessions[day] = status == "open" and close <= as_of
    first, last = date.fromisoformat(start), date.fromisoformat(end)
    requested = [(first + timedelta(days=index)).isoformat() for index in range((last-first).days+1)]
    if not requested or any(day not in sessions for day in requested):
        raise ValueError("calendar range incomplete")
    expected = {day for day in requested if sessions[day]}
    actual = set(frame["Date"].dt.strftime("%Y-%m-%d"))
    if not expected or actual != expected:
        raise ValueError("current cache window incomplete")


def encode(snapshot, descriptor, start, end, calendar, *, store=None):
    provider, instrument = binding(snapshot.symbol, snapshot.provider, descriptor, store=store)
    evidence = calendar_evidence(calendar)
    validate_coverage(snapshot.frame, evidence, start, end, symbol=snapshot.symbol)
    status = snapshot.current_status_provenance
    metadata = {
        "contract": CONTRACT, "provider": asdict(provider), "instrument": asdict(instrument),
        "window": [start, end], "data_usage_mode": get_config()["data_usage_mode"],
        "calendar": evidence,
        "snapshot": {key: getattr(snapshot, key) for key in _SNAPSHOT_FIELDS},
        "status": ({"provider": status.provider, "provider_dataset_id": status.provider_dataset_id,
                    "session_date": status.session_date.isoformat(), "status": status.status.value,
                    "observed_at": status.observed_at.isoformat(), "revision_id": status.revision_id} if status else None),
        "closes": [str(value) if value is not None else None for value in
                   (snapshot.latest_traded_close, snapshot.carried_suspension_close)],
        "source_artifact": snapshot.source_artifact.model_dump(mode="json") if snapshot.source_artifact else None,
        "status_artifact": snapshot.status_artifact.model_dump(mode="json") if snapshot.status_artifact else None,
        "outcomes": [item.model_dump(mode="json") for item in snapshot.acquisition_outcomes],
        "quarantined": [asdict(item) for item in snapshot.quarantined],
    }
    return metadata, provider, instrument


def decode(loaded, descriptor, symbol, start, end, minimum_rows=1):
    """Decode actual retained evidence; never synthesize a new acquisition timestamp."""
    from tradingagents.dataflows.market_snapshot import (
        AuthoritativeMarketSnapshot,
        QuarantinedSnapshot,
        _frame_sha256,
        _live_snapshot_identity,
        _normalized_frame_text,
        validate_ohlcv_frame,
    )
    metadata, stored = loaded.metadata, loaded.stored
    keys = {"contract", "provider", "instrument", "window", "data_usage_mode", "calendar", "snapshot",
            "status", "closes", "source_artifact", "status_artifact", "outcomes", "quarantined"}
    if not isinstance(metadata, dict) or set(metadata) != keys or metadata["contract"] != CONTRACT:
        raise ValueError("legacy or invalid current cache evidence")
    provider, instrument = binding(symbol, stored.provider_name, descriptor)
    instrument = _compatible_instrument_id(instrument, stored.instrument_id)
    if (metadata["provider"] != asdict(provider) or metadata["instrument"] != asdict(instrument)
            or metadata["window"] != [start, end] or stored.requested_start != start or stored.requested_end != end
            or metadata["data_usage_mode"] != get_config()["data_usage_mode"]
            or not stored.current_only or stored.provider_dataset_id != provider.provider_dataset_id
            or stored.instrument_id != instrument.instrument_id or stored.adjustment_basis != descriptor.adjustment_basis
            or stored.snapshot_id != "current-cache-record=sha256:" + digest(metadata)):
        raise ValueError("current cache identity mismatch")
    values = dict(metadata["snapshot"])
    if (set(values) != set(_SNAPSHOT_FIELDS) or values["symbol"] != symbol or values["provider"] != stored.provider_name
            or values["adjustment_basis"] != descriptor.adjustment_basis or values["requested_date"] != end
            or values["retrieved_at"] != stored.retrieved_at or values["frame_sha256"] != stored.frame_digest
            or values["effective_trading_date"] != stored.effective_trading_date):
        raise ValueError("current cache snapshot header mismatch")
    observed = datetime.fromisoformat(values["retrieved_at"])
    max_age = get_config().get("market_history_current_cache_max_age_seconds", 86400)
    if type(max_age) is not int or not 1 <= max_age <= 86400:
        raise ValueError("current cache age must be 1..86400 seconds")
    if observed.tzinfo is None or not timedelta(0) <= _now() - observed <= timedelta(seconds=max_age):
        raise ValueError("current cache expired or future dated")
    frame = validate_ohlcv_frame(loaded.frame, end, start)
    if (len(frame) < minimum_rows or len(frame) != stored.history_rows
            or _frame_sha256(frame) != stored.frame_digest
            or frame["Date"].max().strftime("%Y-%m-%d") != stored.effective_trading_date):
        raise ValueError("current cache frame mismatch")
    validate_coverage(frame, metadata["calendar"], start, end, symbol=symbol)
    artifact = SourceArtifact.model_validate(metadata["source_artifact"])
    if artifact.raw_text != _normalized_frame_text(frame) or artifact.artifact_sha256 != stored.frame_digest:
        raise ValueError("current cache source artifact mismatch")
    status = metadata["status"]
    if status is not None:
        status = TradingStatusProvenance(**{**status, "session_date": date.fromisoformat(status["session_date"]),
            "status": TradingStatus(status["status"]), "observed_at": datetime.fromisoformat(status["observed_at"])})
    latest, carried = (Decimal(value) if value is not None else None for value in metadata["closes"])
    snapshot = AuthoritativeMarketSnapshot(**values, frame=frame, source_artifact=artifact,
        status_artifact=SourceArtifact.model_validate(metadata["status_artifact"]) if metadata["status_artifact"] else None,
        current_status_provenance=status, latest_traded_close=latest, carried_suspension_close=carried,
        acquisition_outcomes=tuple(TypeAdapter(SourceAcquisitionOutcome).validate_python(item) for item in metadata["outcomes"]),
        quarantined=tuple(QuarantinedSnapshot(**{**item, "row_indices": tuple(item["row_indices"])}) for item in metadata["quarantined"]))
    identity = _live_snapshot_identity(snapshot, snapshot.frame_sha256)
    if (snapshot.snapshot_id != identity.snapshot_id or snapshot.snapshot_manifest_json != identity.manifest_json
            or snapshot.pin_membership_digest != identity.membership_digest):
        raise ValueError("current cache snapshot evidence identity mismatch")
    return replace(snapshot, history_store_status="current_only_cached", history_local_rows=len(frame),
                   history_store_diagnostic="retained_acquisition_evidence;no_new_price_request")


def try_read(symbol, start, end, provider_chain, descriptors, minimum_rows):
    runtime = get_config()
    if runtime.get("market_history_mode") != MarketHistoryMode.CURRENT.value or resolve_mainland_instrument(symbol) is None:
        return None, None
    diagnostic = compatibility_diagnostic()
    if diagnostic:
        return None, diagnostic
    if runtime.get("data_usage_mode") != "personal_research":
        return None, "current_cache_usage_not_entitled"
    diagnostics = []
    try:
        with MarketHistoryStore.open(MarketHistoryConfig.from_mapping(runtime)) as store:
            for name in provider_chain:
                # Tushare CURRENT uses the durable raw family, including explicit
                # repairs and bounded refresh; an exact adjusted frame cannot
                # short-circuit that policy.
                if name == "tushare":
                    continue
                descriptor = descriptors.get(name)
                if descriptor is None:
                    continue
                try:
                    provider, instrument = binding(symbol, name, descriptor, store=store)
                    loaded = store.load_current_provider_frame(instrument.canonical_symbol, start, end,
                        minimum_history_rows=minimum_rows, provider=provider, instrument=instrument,
                        adjustment_basis=descriptor.adjustment_basis)
                    return decode(loaded, descriptor, symbol, start, end, minimum_rows), None
                except Exception as exc:
                    diagnostics.append(f"{name}:{type(exc).__name__}:{exc}")
    except Exception as exc:
        diagnostics.append(f"store:{type(exc).__name__}:{exc}")
    return None, "current_cache_miss:" + ";".join(diagnostics) if diagnostics else None


def persist(snapshot, descriptor, start, end, calendar):
    from tradingagents.market_history.shadow import (
        ShadowWriteResult,
        persist_mainland_provider_frame,
    )
    try:
        with MarketHistoryStore.open(MarketHistoryConfig.from_mapping(get_config())) as store:
            metadata, provider, instrument = encode(snapshot, descriptor, start, end, calendar, store=store)
        result = persist_mainland_provider_frame(symbol=snapshot.symbol, provider=snapshot.provider,
            adjustment_basis=snapshot.adjustment_basis, requested_start=start, requested_end=end,
            effective_trading_date=snapshot.effective_trading_date, retrieved_at=snapshot.retrieved_at,
            snapshot_id="current-cache-record=sha256:" + digest(metadata), frame_digest=snapshot.frame_sha256,
            history_rows=len(snapshot.frame), normalized_frame_text=snapshot.source_artifact.raw_text,
            cache_provider=provider, cache_instrument=instrument, snapshot_metadata=metadata)
        if result.persisted:
            with MarketHistoryStore.open(MarketHistoryConfig.from_mapping(get_config())) as store:
                restored = decode(store.load_provider_frame_revision(result.frame_revision_id), descriptor,
                                   snapshot.symbol, start, end)
            if restored.snapshot_id != snapshot.snapshot_id:
                raise ValueError("current cache roundtrip identity mismatch")
        return result
    except Exception as exc:
        return ShadowWriteResult(True, False, diagnostic=f"current_cache_write_failed:{type(exc).__name__}:{exc}")

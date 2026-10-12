"""Durable Tushare CURRENT raw history, separate from qualified PIT snapshots.

Only committed bundle membership is coverage. Acquisitions finish before one
publication; immutable revisions and the store's publisher mutex protect old
evidence and reject a stale concurrent writer rather than lose its peer's work.
"""
from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field, replace
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal

import pandas as pd

from tradingagents.market_history.models import ProvenanceClass

CONTRACT = "tushare-current-raw-history-v1"


@dataclass(frozen=True)
class RetainedHistory:
    bundle_revision_id: str
    observed_at: datetime
    raw: dict
    statuses: dict
    factors: dict
    calendar: dict
    source_digests: tuple[str, ...]
    calendar_revision_ids: tuple[str, ...] = ()
    calendar_observed_at: datetime | None = None
    raw_observed_at: dict[date, datetime] = field(default_factory=dict)


def read_retained(store, provider, instrument, *, bundle_revision_id=None, calendar_revision_ids=None):
    """Read exact family membership, validating every referenced payload digest."""
    if provider.provider_name != "tushare" or provider.strict_history_qualified:
        raise ValueError("only unqualified Tushare CURRENT history is supported")
    connection = store._connection
    row = connection.execute(
        "SELECT b.bundle_revision_id,b.observed_at,r.payload_digest "
        "FROM history_bundle_revisions b JOIN ingestion_runs r ON r.run_id=b.ingestion_run_id "
        "WHERE b.provider_dataset_id=? AND b.instrument_id=? AND (? IS NULL OR b.bundle_revision_id=?) "
        "ORDER BY b.rowid DESC LIMIT 1",
        (provider.provider_dataset_id, instrument.instrument_id, bundle_revision_id, bundle_revision_id),
    ).fetchone()
    if row is None:
        return None
    from tradingagents.market_history.store import _unique_json_object
    payload = json.loads(store.read_payload(row[2]), object_pairs_hook=_unique_json_object)
    if (payload.get("contract") != CONTRACT or payload.get("provider") != asdict(provider)
            or payload.get("instrument") != asdict(instrument)):
        raise ValueError("CURRENT raw family binding mismatch")
    raw, statuses, factors, digests = {}, {}, {}, {row[2]}
    raw_observed_at = {}
    from tradingagents.market_history.bundle import _decimal_text
    from tradingagents.market_history.revisions import RevisionKind, revision_identity
    family = {"instrument_id": instrument.instrument_id, "provider_dataset_id": provider.provider_dataset_id}
    for item in connection.execute(
        "SELECT o.session_date,o.open_value,o.high_value,o.low_value,o.close_value,o.volume_value,o.payload_digest,o.revision_id,o.observed_at "
        "FROM history_bundle_observation_revisions m JOIN raw_market_observation_revisions o "
        "ON o.revision_id=m.revision_id WHERE m.bundle_revision_id=?", (row[0],)
    ):
        raw[date.fromisoformat(item[0])] = tuple(Decimal(value) for value in item[1:6])
        raw_observed_at[date.fromisoformat(item[0])] = datetime.fromisoformat(item[8])
        expected = revision_identity(RevisionKind.RAW_MARKET_OBSERVATION,
            {**family, "session_date": item[0], "payload_digest": item[6],
             **{key: _decimal_text(Decimal(value)) for key, value in zip(
                 ("open", "high", "low", "close", "volume"), item[1:6], strict=True)}})
        if item[7] != expected:
            raise ValueError("CURRENT observation revision integrity failure")
        digests.add(item[6])
    for item in connection.execute(
        "SELECT s.session_date,s.trading_status,s.revision_id,s.observed_at,s.payload_digest,s.volume_value,s.official_carried_close_value "
        "FROM history_bundle_trading_status_revisions m JOIN trading_status_revisions s "
        "ON s.revision_id=m.revision_id WHERE m.bundle_revision_id=?", (row[0],)
    ):
        statuses[date.fromisoformat(item[0])] = item[1:4]
        expected = revision_identity(RevisionKind.TRADING_STATUS,
            {**family, "session_date": item[0], "payload_digest": item[4], "status": item[1],
             "volume": _decimal_text(Decimal(item[5])) if item[5] is not None else None,
             "official_carried_close": _decimal_text(Decimal(item[6])) if item[6] is not None else None})
        if item[2] != expected:
            raise ValueError("CURRENT status revision integrity failure")
        digests.add(item[4])
    for item in connection.execute(
        "SELECT f.effective_date,f.factor_value,f.payload_digest,f.revision_id "
        "FROM history_bundle_adjustment_factor_revisions m JOIN adjustment_factor_revisions f "
        "ON f.revision_id=m.revision_id WHERE m.bundle_revision_id=?", (row[0],)
    ):
        factors[date.fromisoformat(item[0])] = Decimal(item[1])
        if item[3] != revision_identity(RevisionKind.ADJUSTMENT_FACTOR,
            {**family, "effective_date": item[0], "factor": _decimal_text(Decimal(item[1])), "payload_digest": item[2]}):
            raise ValueError("CURRENT factor revision integrity failure")
        digests.add(item[2])
    for digest in digests:
        store.read_payload(digest)
    calendar = {date.fromisoformat(day): value for day, value in payload["calendar"].items()}
    from tradingagents.dataflows.tushare_market_history import (
        CALENDAR_DATASET_NAME,
        NORMALIZER_VERSION,
        _identity,
    )
    exchange = "SSE" if instrument.canonical_symbol.endswith(".SS") else "SZSE"
    dataset = _identity("provider-dataset", provider.upstream_service_id, CALENDAR_DATASET_NAME,
                        "not-applicable", NORMALIZER_VERSION, exchange)
    available_calendars = connection.execute(
        "SELECT calendar_revision_id,payload_digest,observed_at FROM market_session_calendars "
        "WHERE provider_dataset_id=? ORDER BY observed_at,rowid", (dataset,)).fetchall()
    selected = []
    calendar_observed_at = datetime.fromisoformat(row[1])
    for revision, digest, observed_at in available_calendars:
        if calendar_revision_ids is not None and revision not in calendar_revision_ids:
            continue
        proof = json.loads(store.read_payload(digest), object_pairs_hook=_unique_json_object)
        if proof.get("normalizer_version") != NORMALIZER_VERSION:
            raise ValueError("CURRENT calendar normalizer mismatch")
        for entry in proof["calendar"]:
            day = datetime.strptime(entry["cal_date"], "%Y%m%d").date()
            flag = Decimal(entry["is_open"])
            if entry["exchange"] != exchange or flag not in (0, 1):
                raise ValueError("CURRENT calendar source binding mismatch")
            normalized_flag = str(int(flag))
            if day in calendar and calendar[day] != normalized_flag:
                raise ValueError("CURRENT calendar revisions conflict")
            calendar[day] = normalized_flag
        selected.append(revision)
        digests.add(digest)
        calendar_observed_at = max(calendar_observed_at, datetime.fromisoformat(observed_at))
    if calendar_revision_ids is not None and set(selected) != set(calendar_revision_ids):
        raise ValueError("CURRENT calendar revision unavailable")
    if set(raw) != set(statuses) or not set(raw).issubset(factors):
        raise ValueError("CURRENT raw/status/factor membership incomplete")
    if any(value[0] != "traded" for value in statuses.values()):
        raise ValueError("Tushare CURRENT cannot infer official suspension closes")
    return RetainedHistory(row[0], datetime.fromisoformat(row[1]), raw, statuses,
                           factors, calendar, tuple(sorted(digests)), tuple(selected), calendar_observed_at, raw_observed_at)


def _days(start, end):
    return tuple(start + timedelta(days=index) for index in range((end-start).days+1))


def _ranges(days, sessions):
    """Group adjacent *open sessions* without bridging retained open sessions."""
    groups = []
    group = []
    for day in sorted(sessions):
        if day in days:
            group.append(day)
        elif group:
            groups.append((group[0], group[-1]))
            group = []
    if group:
        groups.append((group[0], group[-1]))
    return tuple(groups)


def _materialize(retained, symbol, start, end):
    from tradingagents.dataflows.tushare_market_history import (
        _candidate,
        _instrument,
        history_binding,
    )
    binding, instrument = history_binding(symbol)
    security = _instrument(symbol)
    code = security.yahoo_symbol.replace(".SS", ".SH")
    exchange = "SSE" if security.exchange == "shanghai" else "SZSE"
    raw = pd.DataFrame([
        dict(zip(("ts_code", "trade_date", "open", "high", "low", "close", "vol"),
                 (code, day.strftime("%Y%m%d"), *values[:4], values[4]/100), strict=True))
        for day, values in sorted(retained.raw.items()) if start <= day <= end
    ])
    factors = pd.DataFrame([{"ts_code": code, "trade_date": day.strftime("%Y%m%d"), "adj_factor": value}
                            for day, value in sorted(retained.factors.items()) if day <= end])
    calendar = pd.DataFrame([{"exchange": exchange, "cal_date": day.strftime("%Y%m%d"), "is_open": value}
                             for day, value in sorted(retained.calendar.items()) if start <= day <= end])
    candidate = _candidate(raw, factors, pd.DataFrame(), calendar, binding=binding,
                           instrument=instrument, ts_code=code, exchange=exchange, start=start, end=end,
                           observed_at=retained.observed_at)
    last = max(item.session_date for item in candidate.history_bundle.observations)
    status = retained.statuses[last]
    proof = json.dumps({"contract": "tushare-current-view-v1", "provider": asdict(binding),
        "instrument": asdict(instrument), "bundle_revision_id": retained.bundle_revision_id,
        "source_digests": retained.source_digests, "calendar_revision_ids": retained.calendar_revision_ids,
        "start": start.isoformat(), "end": end.isoformat()}, sort_keys=True, separators=(",", ":")).encode()
    return replace(candidate,
        history_bundle=replace(candidate.history_bundle, observed_at=retained.observed_at,
                               retrieval_cutoff=retained.observed_at, raw_payload=proof),
        calendar=replace(candidate.calendar, observed_at=retained.calendar_observed_at or retained.observed_at, raw_payload=proof,
                         payload_contract="tushare-current-view-v1"),
        current_status_provenance=replace(candidate.current_status_provenance,
            observed_at=datetime.fromisoformat(status[2]), revision_id=status[1]),
        bundle_revision_id=retained.bundle_revision_id,
        history_local_rows=sum(start <= day <= end for day in retained.raw))


def load_current_history(store, symbol, start_date, end_date, *, acquire, acquire_calendar,
                         max_age_seconds=86400, repair_ranges=(), force_refresh=False, now=None):
    """Acquire gaps/repairs and bounded revisions, commit once, reread full window.

    Callbacks are transport seams; callers supply the existing provider authority.
    No client, environment, credentials, limiter or database is created here.
    """
    from tradingagents.dataflows.tushare_market_history import history_binding
    from tradingagents.market_history.current import _analysis_as_of
    start, end = date.fromisoformat(start_date), date.fromisoformat(end_date)
    if start > end or type(force_refresh) is not bool:
        raise ValueError("invalid CURRENT request")
    if type(max_age_seconds) is not int or not 1 <= max_age_seconds <= 86400:
        raise ValueError("CURRENT refresh age must be 1..86400 seconds")
    repairs = []
    for pair in repair_ranges:
        if not isinstance(pair, (list, tuple)) or len(pair) != 2:
            raise ValueError("repair ranges must be [start,end] pairs")
        first, last = (date.fromisoformat(value) for value in pair)
        if first > last or first < start or last > end:
            raise ValueError("repair ranges must lie within the requested window")
        repairs.append((first, last))
    now = now or datetime.now(timezone.utc)
    provider, instrument = history_binding(symbol)
    retained = store.load_current_history(provider, instrument)
    local_rows = sum(start <= day <= end for day in retained.raw) if retained else 0
    calendar = dict(retained.calendar) if retained else {}
    pieces = []
    discovered_calendar = None
    if retained is None:
        candidate = acquire(start_date, end_date, end_date)
        pieces.append(candidate)
        ranges = ((start, end),)
        calendar.update({item.session_date: "1" if item.status.value == "open" else "0"
                         for item in candidate.calendar.sessions})
    else:
        missing_calendar = set(_days(start, end)) - set(calendar)
        if missing_calendar:
            discovered_calendar = acquire_calendar(min(missing_calendar).isoformat(), max(missing_calendar).isoformat())
            calendar.update({item.session_date: "1" if item.status.value == "open" else "0"
                             for item in discovered_calendar.sessions})
        if any(day not in calendar for day in _days(start, end)):
            raise ValueError("CURRENT calendar window incomplete")
        cutoff = _analysis_as_of(end, timezone_name="Asia/Shanghai")
        sessions = [day for day in _days(start, end) if calendar[day] == "1"
                    and datetime.fromisoformat(day.isoformat()+"T15:00:00+08:00") <= cutoff]
        missing = set(sessions) - set(retained.raw)
        tail_observations = [retained.raw_observed_at[day] for day in sessions[-21:] if day in retained.raw]
        if retained.observed_at > now or any(observed > now for observed in tail_observations):
            raise ValueError("CURRENT retained evidence is future dated")
        due = force_refresh or any(now-observed > timedelta(seconds=max_age_seconds)
                                  for observed in tail_observations) or bool(
            sessions and sessions[-1] > max(retained.raw))
        required = set(missing)
        if due:
            required.update(sessions[-21:])
        required.update(day for day in sessions if any(a <= day <= b for a, b in repairs))
        ranges = _ranges(required, sessions)
        factor_end = max(end, max(retained.raw)).isoformat()
        for first, last in ranges:
            pieces.append(acquire(first.isoformat(), last.isoformat(), factor_end))
    if not pieces:
        if discovered_calendar is not None:
            # This commits only provider-returned calendar membership. It adds
            # no price/status/factor coverage and does not renew raw freshness.
            _materialize(replace(retained, calendar=calendar), symbol, start, end)
            store.publish_session_calendar(discovered_calendar)
            retained = store.load_current_history(provider, instrument)
        return _materialize(retained, symbol, start, end)
    observations, statuses = {}, {}
    factor_values = None
    for candidate in pieces:
        publication = candidate.history_bundle
        if (publication.provider, publication.instrument) != (provider, instrument):
            raise ValueError("CURRENT acquired family mismatch")
        source_payload = json.loads(publication.raw_payload)
        candidate_factors = {datetime.strptime(item["trade_date"], "%Y%m%d").date(): Decimal(item["adj_factor"])
                             for item in source_payload["adjustment_factors"]}
        # Different request ends have different normalized anchors. Normalize
        # every complete factor response to the same retained-history anchor.
        anchor = max(candidate_factors)
        normalized = {day: value/candidate_factors[anchor] for day, value in candidate_factors.items()}
        if factor_values is not None and normalized != factor_values:
            raise ValueError("factor revision changed within atomic CURRENT acquisition")
        factor_values = normalized
        observations.update({item.session_date: item for item in publication.observations})
        statuses.update({item.session_date: item for item in publication.trading_statuses})
        calendar.update({item.session_date: "1" if item.status.value == "open" else "0"
                         for item in candidate.calendar.sessions})
    retained_dates = set(retained.raw) if retained else set()
    if not (retained_dates | set(observations)).issubset(factor_values):
        raise ValueError("factor revision does not cover all retained raw history")
    from tradingagents.market_history.models import AdjustmentFactorObservation
    retrospective = ProvenanceClass.RETROSPECTIVE_BACKFILL
    publication = replace(pieces[-1].history_bundle,
        requested_as_of=max(end, max(retained_dates | set(observations))),
        observations=tuple(replace(item, provenance_class=retrospective, provider_available_at=None)
                           for _, item in sorted(observations.items())),
        trading_statuses=tuple(replace(item, provenance_class=retrospective, provider_available_at=None)
                               for _, item in sorted(statuses.items())),
        adjustment_factors=tuple(AdjustmentFactorObservation(day, value, provenance_class=retrospective)
                                 for day, value in sorted(factor_values.items())),
        provenance_class=retrospective,
        raw_payload=json.dumps({"contract": CONTRACT, "provider": asdict(provider),
            "instrument": asdict(instrument), "calendar": {day.isoformat(): value for day, value in sorted(calendar.items())},
            "source_responses": [json.loads(item.history_bundle.raw_payload) for item in pieces],
            "calendar_response": json.loads(discovered_calendar.raw_payload) if discovered_calendar else None,
            "retained_source_digests": list(retained.source_digests) if retained else []},
            sort_keys=True, separators=(",", ":")).encode())
    # Validate the *entire requested view* before it can become coverage.
    raw = dict(retained.raw) if retained else {}
    raw.update({day: (item.open,item.high,item.low,item.close,item.volume) for day,item in observations.items()})
    status_rows = dict(retained.statuses) if retained else {}
    status_rows.update({day: (item.status.value, "pending", publication.observed_at.isoformat())
                        for day,item in statuses.items()})
    proposed = RetainedHistory("pending", publication.observed_at, raw, status_rows, factor_values, calendar, ())
    _materialize(proposed, symbol, start, end)
    committed = store.publish_current_history(publication,
        prior_bundle_revision_id=retained.bundle_revision_id if retained else None)
    restored = store.load_current_history(provider, instrument)
    if restored.bundle_revision_id != committed.bundle_revision_id:
        raise ValueError("CURRENT head changed before readback")
    return replace(_materialize(restored, symbol, start, end), history_local_rows=local_rows,
                   history_requested_ranges=tuple((a.isoformat(), b.isoformat()) for a,b in ranges))

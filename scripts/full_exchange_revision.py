"""Verify the run-owned native Tushare calendar against the originally admitted wire body."""
import json
from datetime import datetime
from hashlib import sha256
from types import SimpleNamespace

def verify(store, revision, symbol, wire):
    import pandas as pd
    from daily_retention import unique
    from tradingagents.dataflows.tushare_market_history import load_calendar
    from tradingagents.market_history.calendar import _session_fields
    from tradingagents.market_history.revisions import RevisionKind, revision_identity
    obj = json.loads(wire, object_pairs_hook=unique)
    data = obj["data"]
    if type(obj["code"]) is not int or obj["code"] != 0 or data["fields"] != ["exchange","cal_date","is_open","pretrade_date"] or data.get("has_more", False) is not False:
        raise ValueError("calendar_wire_schema")
    frame = pd.DataFrame(data["items"], columns=data["fields"])
    dates = [datetime.strptime(value,"%Y%m%d").date() for value in frame.cal_date]
    if len(dates) != 8 or (max(dates)-min(dates)).days != 7:
        raise ValueError("calendar_window_mismatch")
    class Replay:
        def fetch_range(self, endpoint, *, exchange, start_date, end_date, request_executor):
            return request_executor(endpoint, lambda: frame)
    pub = load_calendar(symbol, min(dates).isoformat(), max(dates).isoformat(), provider=Replay())
    provider = pub.provider
    digest = sha256(pub.raw_payload).hexdigest()
    expected = revision_identity(RevisionKind.MARKET_SESSION_CALENDAR, dict(
        payload_digest=digest, provider_dataset_id=provider.provider_dataset_id, reference_market=pub.reference_market,
        sessions=_session_fields(pub.sessions), timezone_name=pub.timezone_name))
    con = store._connection
    row = con.execute("""SELECT c.observed_at,c.first_observed_at,c.ingestion_run_id,c.provider_dataset_id,
        c.reference_market,c.timezone_name,c.provenance_class,c.payload_digest,d.provider_name,d.dataset_name,
        d.adjustment_methodology,d.strict_history_qualified,d.upstream_service_id,u.service_name,u.account_scope
        FROM market_session_calendars c JOIN provider_datasets d USING(provider_dataset_id)
        JOIN upstream_services u USING(upstream_service_id) WHERE c.calendar_revision_id=?""",(revision,)).fetchone()
    if expected != revision or row is None or row[0]!=row[1] or tuple(row[3:]) != (
        provider.provider_dataset_id,pub.reference_market,pub.timezone_name,pub.provenance_class.value,digest,
        provider.provider_name,provider.dataset_name,"not-applicable",0,provider.upstream_service_id,
        provider.upstream_service_name,"") or store.read_payload(digest)!=pub.raw_payload:
        raise ValueError("calendar_revision_mismatch")
    actual = con.execute("""SELECT session_date,session_status,opens_at,closes_at FROM market_sessions
        WHERE calendar_revision_id=? ORDER BY session_date""",(revision,)).fetchall()
    if actual != [(s.session_date.isoformat(),s.status.value,None,None) for s in pub.sessions]:
        raise ValueError("calendar_membership_conflict")
    ingestion = revision_identity(RevisionKind.INGESTION_RUN,dict(calendar_revision_id=revision,observed_at=row[0],operation="calendar_refresh"))
    trace = con.execute("""SELECT provider_dataset_id,instrument_id,operation,provenance_class,requested_start,
        requested_end,started_at,completed_at,status,payload_digest,error_code FROM ingestion_runs WHERE run_id=?""",(ingestion,)).fetchone()
    if ingestion!=row[2] or trace!=(provider.provider_dataset_id,None,"calendar_refresh",pub.provenance_class.value,
          min(dates).isoformat(),max(dates).isoformat(),row[0],row[0],"published",digest,None):
        raise ValueError("calendar_ingestion_mismatch")
    return SimpleNamespace(sessions=pub.sessions,calendar_revision_id=revision,observed_at=datetime.fromisoformat(row[0]))

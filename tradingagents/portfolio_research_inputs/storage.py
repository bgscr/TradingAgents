"""Persist normalized observations and link their immutable digests into the active Full ledger."""
from datetime import date,datetime,time,timezone
from hashlib import sha256
import json
from threading import Lock
from zoneinfo import ZoneInfo

from tradingagents.full_session_ledger import owner
from tradingagents.portfolio_research_run import active_selection
from tradingagents.report_storage import ReportStore
from . import current_facts, market_fact

_lock=Lock()
_ORIGIN="portfolio-research-producer-v1"
_FIRST="initial"
_PROGRESS="portfolio-research-control-v1"


def canonical(value):
    return json.dumps(value,ensure_ascii=False,sort_keys=True,separators=(",",":"),allow_nan=False).encode()


def project(kind,payload,run,digest,*,latest_observed_by=None):
    day=date.fromisoformat(run.as_of)
    cutoff=datetime.combine(day,time(16),ZoneInfo("Asia/Shanghai"))
    if latest_observed_by is not None:
        if latest_observed_by.utcoffset() is None or latest_observed_by < cutoff:
            raise ValueError("invalid_current_research_observation_time")
        cutoff=latest_observed_by
    if kind=="financial":
        rows=current_facts(payload,owner_sha256=digest,symbol=run.symbol,as_of=day,observed_by=cutoff)
        return {key:value.model_dump(mode="json") for key,value in rows.items()}
    if kind!="market":
        raise ValueError("unknown_research_source")
    from tradingagents.dataflows.market_snapshot import SNAPSHOT_PROVIDERS
    if (payload.get("market_snapshot") or {}).get("provider") not in SNAPSHOT_PROVIDERS:
        return {}
    fact=market_fact(payload,owner_sha256=digest,symbol=run.symbol,as_of=day,observed_by=cutoff)
    return {fact.fact_id:fact.model_dump(mode="json")} if fact else {}


def link_key(kind, predecessor):
    return "portfolio_research/"+kind+"/"+predecessor


def history(store,current,run,kind):
    records={name:checksum for name,checksum,origin,cost in current.snapshot()["evidence"]
             if origin in {_ORIGIN,_PROGRESS} and cost==0 and name.startswith("portfolio_research/"+kind+"/")}
    previous=_FIRST
    tip=None
    while records:
        checksum=records.pop(link_key(kind,previous),None)
        if checksum is None:
            raise ValueError("broken_research_source_chain")
        raw=store.read_bytes("research-evidence/"+checksum+".json")
        if raw is None or sha256(raw).hexdigest()!=checksum:
            raise ValueError("research_observation_digest_changed")
        tip=json.loads(raw)
        if (tip["owner"],tip["selection"],tip["symbol"],tip["as_of"],tip["kind"],tip["previous"])!=(
                current.key,run.selection_sha256,run.symbol,run.as_of,kind,previous):
            raise ValueError("research_observation_binding")
        previous=tip["payload_sha256"]
    return tip


def seal(kind,payload,*,symbol,day,previous=None):
    active=active_selection()
    if active is None:
        return None
    current,run=active
    if (symbol,day)!=(run.symbol,run.as_of):
        raise ValueError("research_producer_selection_changed")
    digest=sha256(canonical(payload)).hexdigest()
    if current.params.get("requested_date") != "latest" or current.params.get("mode") != "default_full":
        raise ValueError("research_current_owner_required")
    observed_by=datetime.now(timezone.utc)
    observations=project(kind,payload,run,digest,latest_observed_by=observed_by)
    if not observations and kind!="financial":
        return None
    # A dispatcher-produced unavailable checkpoint advances anti-replay state only.
    # It has no observations and must never be returned as financial evidence.
    if not observations:
        from tradingagents.dataflows.financial_dispatch import FinancialDispatchCheckpointLedger
        FinancialDispatchCheckpointLedger.model_validate(payload)
    parent=sha256(canonical(previous)).hexdigest() if previous is not None else _FIRST
    with _lock,ReportStore(current.folder.parent) as store:
        tip=history(store,current,run,kind)
        if tip and tip["payload_sha256"]==digest:
            return digest
        if parent!=(tip["payload_sha256"] if tip else _FIRST):
            raise ValueError("research_checkpoint_predecessor")
        row=dict(version=2,observation_scope="current_retrospective",observed_by=observed_by.isoformat(),owner=current.key,selection=run.selection_sha256,symbol=run.symbol,
                 as_of=run.as_of,kind=kind,previous=parent,payload_sha256=digest,observations=observations)
        raw=canonical(row)
        checksum=sha256(raw).hexdigest()
        path="research-evidence/"+checksum+".json"
        # A prior partial write is never silently registered.
        store.write_bytes(path,raw)
        readback=store.read_bytes(path)
        if readback!=raw:
            raise ValueError("research_persistence_failed")
        current.register(link_key(kind,parent),readback,checksum,_ORIGIN if observations else _PROGRESS,0)
    return digest


def read_current(kind):
    if kind not in {"financial","market"}:
        raise ValueError("unknown_research_source")
    active=active_selection()
    if not active:
        raise ValueError("research_owner_missing")
    current,run=active
    with _lock,ReportStore(current.folder.parent) as store:
        tip=history(store,current,run,kind)
        if tip:
            if tip.get("version")!=2 or tip.get("observation_scope")!="current_retrospective":
                raise ValueError("research_scope_mismatch")
            sealed=datetime.fromisoformat(tip["observed_by"])
            if sealed.utcoffset() is None or sealed<datetime.combine(date.fromisoformat(run.as_of),time(16),ZoneInfo("Asia/Shanghai")):
                raise ValueError("invalid_research_seal_time")
        return tip if tip and tip["observations"] else None

def check_previous(kind,previous,*,symbol,day):
    active=active_selection()
    if active is None:
        return
    current,run=active
    if (symbol,day)!=(run.symbol,run.as_of) or kind!="financial":
        raise ValueError("research_producer_selection_changed")
    expected=sha256(canonical(previous)).hexdigest() if previous is not None else _FIRST
    with _lock,ReportStore(current.folder.parent) as store:
        tip=history(store,current,run,kind)
        if expected!=(tip["payload_sha256"] if tip else _FIRST):
            raise ValueError("research_checkpoint_predecessor")



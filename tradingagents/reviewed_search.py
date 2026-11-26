"""Optional scoped local archive lookup. External transport is deliberately absent."""
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from datetime import date, datetime, timezone, timedelta
from hashlib import sha256
import json
from urllib.parse import urlsplit
from threading import Lock

_active = ContextVar("reviewed_archive_search", default=None)

@dataclass(frozen=True)
class Scope:
    run: str
    ticker: str
    start: date
    end: date
    cutoff: datetime
    kind: str

@dataclass(frozen=True)
class Grant:
    scope: Scope
    source_host: str
    tier: str
    raw_digest: str
    metadata_digest: str

def required_receipts(grant, selection_digest):
    scope=grant.scope
    scope_bytes=json.dumps([scope.run,scope.ticker,scope.kind,scope.start.isoformat(),scope.end.isoformat(),scope.cutoff.isoformat(),selection_digest],
                           separators=(",",":")).encode()
    key=sha256(scope_bytes).hexdigest()
    auth=sha256(scope_bytes+b"\0"+json.dumps([grant.source_host,grant.tier,grant.raw_digest,grant.metadata_digest],separators=(",",":")).encode()).hexdigest()
    return {("archive-search/cache/"+key,key,"verified-cache-gap",0),
            ("archive-search/provider/"+key,key,"verified-provider-gap",0),
            ("archive-search/grant/"+key,auth,"reviewed-archive-authorization",0)}

@contextmanager
def archive_session(owner, grants, verified_gaps, read_local, budget):
    # Installed only by a trusted caller after independent review of both gaps.
    if _active.get() is not None:
        raise ValueError("nested_search_scope")
    token = _active.set((owner, tuple(grants), frozenset(verified_gaps), read_local, budget, set(), Lock()))
    try:
        yield
    finally:
        _active.reset(token)

def lookup(kind, ticker, start, end, *, primary_missing):
    state = _active.get()
    if state is None or not primary_missing or kind not in {"stock_news","announcement"}:
        return ()
    from tradingagents.full_session_ledger import owner as active_owner
    owner, grants, gaps, read_local, budget, consumed, lock = state
    if active_owner() is not owner:
        return ()
    selected = tuple(g for g in grants if (g.scope.kind,g.scope.ticker,g.scope.start.isoformat(),g.scope.end.isoformat())==(kind,ticker,start,end)
                     and g.scope.run==owner.key and owner.params["ticker"].replace(".SH",".SS")==ticker.replace(".SH",".SS"))
    if len(selected)!=1:
        return ()
    grant,=selected
    scope=grant.scope
    if scope not in gaps or scope.cutoff.utcoffset() is None or scope.start>scope.end:
        return ()
    from tradingagents.portfolio_research_run import active_selection
    bound=active_selection()
    if not bound or bound[0] is not owner:
        return ()
    run=bound[1]
    if (run.run_id,run.symbol,run.as_of)!=(scope.run,scope.ticker.replace(".SH",".SS"),scope.end.isoformat()):
        return ()
    if not required_receipts(grant,run.selection_sha256)<=set(map(tuple,owner.snapshot()["evidence"])):
        return ()
    with lock:
        if scope in consumed:
            return ()
        consumed.add(scope)
    budget.check()  # Local archive only: charge no physical send; at most one read per explicit grant.
    try:
        raw,meta=read_local(grant)
        if type(raw) is not bytes or type(meta) is not bytes or len(raw)>1048576 or len(meta)>16384:
            return ()
        if (sha256(raw).hexdigest(),sha256(meta).hexdigest())!=(grant.raw_digest,grant.metadata_digest):
            return ()
        m=json.loads(meta)
        url=urlsplit(m["source"])
        if (url.scheme!="https" or url.netloc!=grant.source_host or url.username or url.password
                or m["tier"]!=grant.tier or m["tier"] not in ({"issuer","exchange"} if kind=="announcement" else {"media","issuer"})
                or (m["kind"],m["ticker"])!=(kind,ticker)):
            return ()
        published,revision,observed=(datetime.fromisoformat(m[k]) for k in ("published","revision","observed"))
        if any(t.utcoffset() is None for t in (published,revision,observed)):
            return ()
        if scope.start.year < 1992: return ()
        if not scope.start<=published.astimezone(timezone(timedelta(hours=8))).date()<=scope.end or not published<=revision<=observed<=scope.cutoff:
            return ()
        if not all(m[k].encode("utf8") in raw for k in ("ticker","published","revision")):
            return ()
        quote=m["quote"]
        if not isinstance(quote,str) or not 0<len(quote)<=512 or quote.encode() not in raw:
            return ()
        return (dict(ticker=ticker,kind=kind,source=m["source"],tier=m["tier"],published=m["published"],revision=m["revision"],
                     observed=m["observed"],sha256=grant.raw_digest,quoted_data=quote,coverage="partial"),)
    except (KeyError,TypeError,ValueError,OSError):
        return ()





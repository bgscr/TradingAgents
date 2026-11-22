"""Durable envelope for a bounded Full owner, including its preparatory sends."""
import json
import sqlite3
import time
import secrets
from contextlib import closing,contextmanager
from contextvars import ContextVar
from hashlib import sha256
from pathlib import Path
from threading import Lock
from tradingagents import full_dispatch as dispatch

_OWNER = None
_LOCK = Lock()
finance = ContextVar("bounded_full_financial", default=False)

_ROLE=ContextVar("full_local_role",default=None)

def owner():
    return _ROLE.get() or _OWNER

@contextmanager
def preparation_role(role):
    parent=owner()
    if parent is None or parent.child_role is not None:
        raise ValueError("full_owner_required")
    ticket=parent.child_ticket(role)
    child=Ledger.join_child(parent.path,parent.key,parent.params,role,ticket)
    token=_ROLE.set(child)
    try:yield
    finally:_ROLE.reset(token)

@contextmanager
def bind(value):
    global _OWNER
    with _LOCK:
        if _OWNER is not None:raise RuntimeError("concurrent_owner")
        _OWNER=value
    try:yield value
    finally:
        with _LOCK:_OWNER=None

class Ledger:
    def __init__(self,folder,key,params,*,clock=time.monotonic,wall=time.time):
        self.folder=Path(folder);self.path=self.folder/"ledger.sqlite3"
        self.key=key;self.params=params
        self.clock=clock;self.wall=wall
        self.folder.mkdir(exist_ok=False)
        dispatch.initialize(self.path)
        dispatch.prepare(self.path,key,params)
        self.claim=dispatch.claim(self.path,key,params)
        if not self.claim:raise RuntimeError("already_claimed")
        with closing(sqlite3.connect(self.path)) as c,c:
            c.execute("create table children(token text primary key, role text, taken integer)")
            c.execute("create table sends(n integer primary key,phase text,kind text,wall real,mono real)")
            c.execute("create table evidence(source text,digest text,origin text,cost integer,primary key(source,origin))")
        self.child_role=None
        self.phase="preparation"

    def child_ticket(self,role):
        if self.child_role is not None:raise ValueError("owner_only")
        if role not in ("financial","identity","calendar"):raise ValueError("unknown_child")
        token=secrets.token_hex(32)
        with dispatch.connection(self.path,"rw") as c:
            c.execute("begin immediate")
            if dispatch.check(c,self.key,self.params)[1:]!=(self.claim,None):raise ValueError("owner_closed")
            c.execute("insert into children values(?,?,0)",(sha256(token.encode()).hexdigest(),role))
        return token

    @classmethod
    def join_child(cls,path,key,params,role,token):
        # Delivered privately by the already verified local supervisor; do not put capability in logs.
        path=Path(path)
        with dispatch.connection(path,"rw") as c:
            c.execute("begin immediate")
            state=dispatch.check(c,key,params)
            if not state or not state[1] or state[2]:raise ValueError("owner_closed")
            ch=c.execute("update children set taken=1 where token=? and role=? and taken=0",
                         (sha256(token.encode()).hexdigest(),role))
            if ch.rowcount!=1:raise ValueError("child_already_claimed_or_unknown")
        child=object.__new__(cls)
        child.folder=path.parent;child.path=path;child.key=key;child.params=params;child.claim=state[1]
        child.clock=time.monotonic;child.wall=time.time;child.phase=role;child.child_role=role
        return child

    def remaining(self):
        with dispatch.connection(self.path,"ro") as c:
            first=c.execute("select wall,mono from sends where n=1").fetchone()
        if not first:return 1200
        return min(first[0]+1200-self.wall(),first[1]+1200-self.clock())

    def reserve(self,kind):
        if kind not in ("model","financial","data"):raise ValueError("unknown_kind")
        if self.child_role is not None and kind!=("financial" if self.child_role=="financial" else "data"):raise ValueError("child_kind_mismatch")
        from tradingagents.analysis_budget import AnalysisBudgetStopped
        with dispatch.connection(self.path,"rw") as c:
            c.execute("begin immediate")
            status=dispatch.check(c,self.key,self.params)
            if status[1:]!=(self.claim,None):raise AnalysisBudgetStopped("owner_closed")
            now=self.wall();mono=self.clock()
            first=c.execute("select wall,mono from sends where n=1").fetchone() or (now,mono)
            remaining=min(first[0]+1200-now,first[1]+1200-mono)
            n,f=c.execute("select count(*),count(case when kind='financial' then 1 end) from sends").fetchone()
            if remaining<=0:raise AnalysisBudgetStopped("deadline_exceeded")
            if n>=min(60,self.params.get("session_max_calls",60)):raise AnalysisBudgetStopped("call_limit_exceeded")
            if kind=="financial" and f>=3:raise AnalysisBudgetStopped("financial_limit_exceeded")
            c.execute("insert into sends values(?,?,?,?,?)",(n+1,self.phase,kind,now,mono))
        return remaining

    def register(self,source,raw,expected,origin,cost):
        if self.child_role is not None:raise ValueError("owner_only")
        digest=sha256(raw).hexdigest()
        if digest!=expected:raise ValueError("evidence_digest_changed")
        with dispatch.connection(self.path,"rw") as c:
            c.execute("begin immediate")
            if dispatch.check(c,self.key,self.params)[1:]!=(self.claim,None):raise ValueError("owner_closed")
            c.execute("insert into evidence values(?,?,?,?)",(source,digest,origin,cost))

    def snapshot(self):
        with dispatch.connection(self.path,"ro") as c:
            return dict(events=c.execute("select * from sends order by n").fetchall(),
                        evidence=c.execute("select * from evidence order by source").fetchall())

    def reuse(self,source,raw,prior_db,prior_key,prior_params,reviewed_outcome_hash,*,review,review_sha256,target,as_of):
        # This method consumes independently approved review bytes and their trusted deployment pin.
        from datetime import datetime
        from zoneinfo import ZoneInfo
        if sha256(review).hexdigest()!=review_sha256:raise ValueError("unreviewed_material")
        r=json.loads(review)
        if (source!="keywords" or r.get("source")!=source or r.get("use")!="dated_observation"
            or r.get("ticker")!=self.params.get("ticker") or r.get("ticker")!=prior_params.get("ticker")
            or r.get("target")!=target or r.get("origin")!=prior_key or r.get("origin_terminal")!=reviewed_outcome_hash
            or r.get("raw_sha256")!=sha256(raw).hexdigest() or as_of.utcoffset() is None):
            raise ValueError("cache_binding_mismatch")
        observed=datetime.fromisoformat(r["observed_at"])
        if (observed.utcoffset() is None or observed>as_of or
            observed.astimezone(ZoneInfo("Asia/Shanghai")).date().isoformat()!=target):
            raise ValueError("cache_time_mismatch")
        bundle=json.loads(raw)
        if bundle.get("status")!=0 or not bundle.get("data") or any(
            row.get("srcSecurityCode")!=self.params["ticker"][-2:]+self.params["ticker"][:6] or row.get("calcTime","")[:10]!=target
            for row in bundle["data"]):
            raise ValueError("cache_source_mismatch")
        # Prior run and its cost remain visible; only the current ledger is writable.
        status,_,digest=dispatch.lookup(prior_db,prior_key,prior_params)
        if status!="completed" or digest!=reviewed_outcome_hash:raise ValueError("unreviewed_prior")
        with dispatch.connection(prior_db,"ro") as c:
            events=c.execute("select * from sends order by n").fetchall()
            records=c.execute("select * from evidence order by source").fetchall()
        if self.digest(dict(events=events,evidence=records))!=digest:raise ValueError("prior_ledger_changed")
        matched=[r for r in records if r[0]==source and r[2]==prior_key]
        if len(matched)!=1:raise ValueError("missing_origin")
        self.register(source,raw,matched[0][1],prior_key,matched[0][3])

    @staticmethod
    def digest(value):
        return sha256(json.dumps(value,separators=(",",":")).encode()).hexdigest()

    def finish(self,status):
        if self.child_role is not None:raise ValueError("owner_only")
        if status not in ("completed","failed"):
            raise ValueError("invalid terminal status")
        with dispatch.connection(self.path,"rw") as c:
            c.execute("begin immediate")
            binding=dispatch.check(c,self.key,self.params)
            if binding[1:]!=(self.claim,None):raise ValueError("owner_closed")
            snapshot=dict(events=c.execute("select * from sends order by n").fetchall(),
                          evidence=c.execute("select * from evidence order by source").fetchall())
            digest=self.digest(snapshot)
            c.execute("update requests set terminal=? where id=?",(json.dumps([status,digest]),self.key))
        return digest

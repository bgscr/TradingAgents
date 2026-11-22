"""Local protocol experiment. No launcher, providers, credentials or retry function."""
from contextlib import closing, contextmanager
import hashlib
import json
import sqlite3
import uuid


def digest(params):
    # Caller supplies the full, versioned, non-secret JSON request.
    if not isinstance(params, dict):
        raise ValueError("expected request object")
    raw = json.dumps(params, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)
    return hashlib.sha256(raw.encode()).hexdigest()


@contextmanager
def connection(path, mode):
    with closing(sqlite3.connect(path.resolve().as_uri()+"?mode="+mode, uri=True)) as db:
        if mode != "ro":
            db.execute("pragma synchronous=FULL")
        with db:
            yield db


def initialize(path):
    with connection(path,"rwc") as db:
        db.execute("create table requests(id text primary key, params text not null, run text unique, terminal text)")


def prepare(path, key, params):
    # Commit intent before any handoff. Duplicate prepare does not grant permission.
    with connection(path,"rw") as db:
        db.execute("insert into requests(id,params) values(?,?) on conflict(id) do nothing",(key,digest(params)))
        check(db,key,params)


def check(db,key,params):
    row=db.execute("select params,run,terminal from requests where id=?",(key,)).fetchone()
    if row and row[0]!=digest(params):
        raise ValueError("request parameter mismatch")
    return row


def claim(path,key,params):
    # Only the caller receiving this fresh grant may enter execution.
    with connection(path,"rw") as db:
        db.execute("begin immediate")
        row=check(db,key,params)
        if not row or row[1] is not None:
            return None
        run=uuid.uuid4().hex
        db.execute("update requests set run=? where id=?",(run,key))
    return run


def finish(path,key,params,run,outcome,artifact_sha256):
    if outcome not in ("completed","failed") or len(artifact_sha256)!=64 or any(c not in '0123456789abcdef' for c in artifact_sha256):
        raise ValueError("invalid terminal evidence")
    receipt=json.dumps([outcome,artifact_sha256])
    with connection(path,"rw") as db:
        db.execute("begin immediate")
        row=check(db,key,params)
        if not row or not run or row[1]!=run or row[2] not in (None,receipt):
            raise ValueError("terminal binding mismatch")
        db.execute("update requests set terminal=? where id=?",(receipt,key))


def lookup(path,key,params):
    # Read-only recovery: no creation, claim, execution or resend.
    try:
        with connection(path,"ro") as db:
            row=check(db,key,params)
    except sqlite3.Error:
        return "unknown",None,None
    if not row or not row[1]:
        return "unknown",None,None
    if row[2] is None:
        return "accepted_terminal_unknown",row[1],None
    status,sha=json.loads(row[2])
    return status,row[1],sha

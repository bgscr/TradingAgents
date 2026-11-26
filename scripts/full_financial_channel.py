"""Authenticated per-send receipts. Financial worker only writes its existing diagnostic directory."""

import hashlib
import hmac
import json
import os
import re
import threading
import time
from contextlib import contextmanager
from pathlib import Path

ENDPOINTS = {"balancesheet", "income", "cashflow"}


def digest(value):
    return hashlib.sha256(
        json.dumps(
            value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False
        ).encode()
    ).hexdigest()


def signed(key, payload):
    return hmac.digest(bytes.fromhex(key), bytes.fromhex(digest(payload)), "sha256").hex()


def ordinary(path):
    path = Path(path)
    if path.resolve() != path or any(
        p.is_symlink() or (p.exists() and getattr(p.lstat(), "st_file_attributes", 0) & 0x400)
        for p in (path, *path.parents)
    ):
        raise ValueError("nonordinary_full_path")
    return path


def load(path):
    ordinary(path)
    if not path.is_file() or path.stat().st_nlink != 1 or path.stat().st_size > 16384:
        raise ValueError("full_record_invalid")
    return json.loads(path.read_bytes())


def write(path, value):
    ordinary(path)
    tmp = path.with_suffix(".pending")
    with tmp.open("xb") as f:
        f.write(json.dumps(value, allow_nan=False).encode())
        f.flush()
        os.fsync(f.fileno())
    if path.exists():
        raise ValueError("duplicate_full_record")
    tmp.rename(path)


def binding(source, root):
    ordinary(source)
    ordinary(root)
    release = ordinary(source / "config/bounded_mainland_release.json")
    manifest = json.loads(release.read_bytes())
    if (
        manifest.get("contract") != "bounded-mainland-release-v1"
        or Path(manifest.get("deployment_root", "")).resolve() != source
    ):
        raise ValueError("installation_root_mismatch")
    for name in (
        "scripts/full_financial_channel.py",
        "scripts/accept_financial_statements.py",
        "scripts/analyze_mainland_bounded.py",
        "tradingagents/full_session_ledger.py",
    ):
        hashes = [
            r["sha256"]
            for r in manifest["files"]
            if r["root"] == "project" and r["relative"].replace(chr(92), "/") == name
        ]
        if hashes != [hashlib.sha256(ordinary(source / name).read_bytes()).hexdigest()]:
            raise ValueError("full_source_unbound")
    if (
        root.parent != source / "reports/bounded-runs"
        or not re.fullmatch("[0-9a-f]{32}", root.name)
        or load(root / "session.json")
        != dict(
            contract="bounded-mainland-session-v1", run_id=root.name, root=str(root), resume=False
        )
    ):
        raise ValueError("full_run_binding")
    spec = load(root / "full-owner.json")
    if (
        spec["source"] != str(source)
        or spec["run_id"] != root.name
        or spec["params_sha256"] != digest(spec["params"])
        or not re.fullmatch(r"[0-9]{6}\.(?:SH|SZ)", spec["params"]["ticker"])
    ):
        raise ValueError("full_params_binding")
    return spec


def selection_record(spec,root):
    params=spec['params']
    receipt=load(ordinary(root/'resolved-selection.json'))
    selection=receipt['selection']
    if (receipt['run_id']!=spec['run_id'] or receipt['params_sha256']!=spec['params_sha256']
        or selection['security'].replace('.SS','.SH')!=params['ticker'] or selection['venue']!=params['venue']):
        raise ValueError('resolution_binding_mismatch')
    return receipt

def effective_day(spec,root):
    if spec['params'].get('contract')!='default-full-latest-intake-v1':
        return spec['params']['analysis_date']
    day=selection_record(spec,root)['selection']['trading_date']
    financial_context(spec,root,day,owner_side=True)
    return day

def financial_context(spec,root,requested_day,*,owner_side):
    if spec['params'].get('contract')!='default-full-latest-intake-v1':
        if requested_day!=spec['params']['analysis_date']:raise ValueError('financial_date_mismatch')
        return []
    record=selection_record(spec,root)
    if requested_day!=record['selection']['trading_date']:raise ValueError('financial_date_mismatch')
    if owner_side:
        # Verify these exact bytes against the sole active ledger before issuing a ticket.
        from contextlib import closing
        import sqlite3
        path=ordinary(root/'physical-ledger/ledger.sqlite3')
        with closing(sqlite3.connect(path.as_uri()+'?mode=ro',uri=True)) as db:
            db.execute('BEGIN')
            row=db.execute("SELECT digest,origin,cost FROM evidence WHERE source='latest_selection'").fetchall()
            claim=db.execute('SELECT params,run,terminal FROM requests WHERE id=?',(root.name,)).fetchone()
        if row!=[(digest(record),'verified-local-reader',0)] or not claim or not claim[1] or claim[2] is not None or claim[0]!=spec['params_sha256']:
            raise ValueError('resolution_not_admitted')
    return [requested_day,digest(record)]


class Client:
    def __init__(self, source, refresh, contract, stream):
        from session_token_pipe import require_pipe

        require_pipe(stream)
        spec = binding(source, refresh.parent)
        req = contract["request"]
        if (
            refresh.name != "refresh"
            or contract["live"] is not True
            or req["destination"] != "https://api.waditu.com/dataapi"
            or not req["as_of"]
            or req["ticker"].replace(".SS", ".SH") != spec["params"]["ticker"]
            or req["account"] != spec["account"]
            or contract["full_params_sha256"] != spec["params_sha256"]
        ):
            raise ValueError("financial_request_binding")
        self.selection=financial_context(spec,refresh.parent,req["as_of"],owner_side=False)
        line = stream.readline(67)
        if not re.fullmatch(b"[0-9a-f]{64}\n", line):
            raise ValueError("financial_ticket_invalid")
        self.key = line[:-1].decode()
        self.refresh = refresh
        self.params = spec["params_sha256"]
        self.run = spec["run_id"]
        self.deadline = contract["session_monotonic_deadline"]
        self.n = 0
        self.exchange("claim")

    def exchange(self, op):
        if time.monotonic() >= self.deadline:
            raise ValueError("session_deadline_exceeded")
        n = self.n
        self.n += 1
        if (n == 0 and op != "claim") or (n > 0 and op not in ENDPOINTS) or n > 3:
            raise ValueError("financial_limit")
        message = [self.run, self.params, n, op, *self.selection]
        write(
            self.refresh / f"ledger-{n}.req", dict(message=message, mac=signed(self.key, message))
        )
        ack = self.refresh / f"ledger-{n}.ack"
        while not ack.exists():
            denied = self.refresh / "ledger.denied"
            if denied.exists():
                record = load(denied)
                denial = ["denied", self.run, self.params]
                if record.get("mac") != signed(self.key, denial):
                    raise ValueError("financial_denial_invalid")
                raise ValueError("financial_ledger_stopped")
            if time.monotonic() >= self.deadline:
                raise ValueError("financial_ack_timeout")
            time.sleep(0.02)
        value = load(ack)
        if value["message"] != message or not hmac.compare_digest(
            value["mac"], signed(self.key, ["ack", message, value["deadline"]])
        ):
            raise ValueError("financial_ack_invalid")
        remaining = min(self.deadline, value["deadline"]) - time.monotonic()
        if remaining <= 0:
            raise ValueError("session_deadline_exceeded")
        return remaining


@contextmanager
def serve(owner, source, root):
    from tradingagents.full_session_ledger import Ledger

    spec = binding(source, root)
    if (
        owner.key != root.name
        or ordinary(owner.path) != root / "physical-ledger" / "ledger.sqlite3"
        or owner.params != spec["params"]
    ):
        raise ValueError("owner_mismatch")
    if spec["params"].get("contract")=="default-full-latest-intake-v1":
        requested_day=selection_record(spec,root)["selection"]["trading_date"]
        selection=financial_context(spec,root,requested_day,owner_side=True)
    else:
        selection=[]
    key = owner.child_ticket("financial")
    stop = threading.Event()
    claimed = threading.Event()
    errors = []

    def service():
        try:
            child = None
            for n in range(4):
                path = root / "refresh" / f"ledger-{n}.req"
                while not path.exists():
                    if stop.wait(0.02):
                        return
                    if owner.remaining() <= 0:
                        raise ValueError("session_deadline_exceeded")
                obj = load(path)
                msg = obj["message"]
                if (
                    len(msg) != 4 + len(selection)
                    or msg[4:] != selection
                    or msg[:3] != [root.name, spec["params_sha256"], n]
                    or not hmac.compare_digest(obj["mac"], signed(key, msg))
                ):
                    raise ValueError("financial_message_invalid")
                if n == 0:
                    if msg[3] != "claim":
                        raise ValueError("financial_claim_required")
                    child = Ledger.join_child(owner.path, owner.key, owner.params, "financial", key)
                    remaining = child.remaining()
                    claimed.set()
                else:
                    if msg[3] not in ENDPOINTS:
                        raise ValueError("financial_endpoint_scope")
                    remaining = child.reserve("financial")
                first = owner.snapshot()["events"]
                deadline = first[0][4] + 1200 if first else time.monotonic() + remaining
                write(
                    path.with_suffix(".ack"),
                    dict(message=msg, deadline=deadline, mac=signed(key, ["ack", msg, deadline])),
                )
        except BaseException as exc:
            errors.append(type(exc).__name__)
            try:
                write(
                    root / "refresh" / "ledger.denied",
                    dict(mac=signed(key, ["denied", root.name, spec["params_sha256"]])),
                )
            except (OSError, ValueError):
                pass  # Loss of the IPC directory is bounded by the original worker timeout.

    thread = threading.Thread(target=service, daemon=True)
    thread.start()
    try:
        yield key
    finally:
        stop.set()
        thread.join()
        if errors:
            raise ValueError("financial_broker_stopped")
        if not claimed.is_set():
            raise ValueError("financial_child_did_not_claim")

"""Prepare the three retained records for independent content-hash registration."""
from hashlib import sha256
import json
from pathlib import Path

URL = "https://api.waditu.com/dataapi/daily"
CONTRACT = dict(adapter="tushare-daily-close-v1",source=URL,scope="auction_close_price")

def registration(capture_dir):
    names = {"raw_sha256":"response.bin","receipt_sha256":"receipt.json","reservation_sha256":"reservation.json"}
    evidence = {key:(Path(capture_dir)/filename).read_bytes() for key,filename in names.items()}
    receipt = json.loads(evidence["receipt_sha256"])
    request = json.loads(evidence["reservation_sha256"])
    digests = {key:sha256(raw).hexdigest() for key,raw in evidence.items()}
    if (receipt["status"]!="observed_daily_close" or receipt["source"]!=URL
            or request["url"]!=URL or request["method"]!="POST"
            or receipt["response_sha256"]!=digests["raw_sha256"]):
        raise ValueError("retained_records_unqualified")
    row=dict(digests,trading_date=receipt["trading_date"],published_at=None,observed_at=receipt["observed_at"])
    policy=json.dumps(CONTRACT,separators=(",",":")).encode()
    originals={digest:evidence[k] for k,digest in digests.items()}
    policy_hash=sha256(policy).hexdigest()
    originals[policy_hash]=policy
    return originals,dict(ticker="601328.SS",source=URL,rows=[row],contract_sha256=policy_hash)

def collect_in_approved_session(root,target):
    """For a verified existing bounded owner after it has loaded its approved project configuration."""
    import os
    from tradingagents.full_session_ledger import owner
    from tradingagents.analysis_budget import active_budget
    current=owner()
    if (current is None or current.child_role is not None or current.phase!="preparation"
            or current.params.get("ticker")!="601328.SH" or active_budget() is None):
        raise ValueError("verified_full_owner_required")
    from scripts.daily_retention import capture
    receipt=capture(root,target,os.environ.get("TUSHARE_TOKEN"))
    if receipt["status"]!="observed_daily_close":
        return None
    return registration(Path(root)/("daily-601328-"+target.isoformat()))
